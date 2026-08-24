"""Talking to a Buzz relay over its NIP-98 HTTP bridge.

Buzz is the collaboration substrate: identities, channels, threads, mentions and
an operational event log. DUM-E uses it and does not reimplement it — but it
also does not grant it authority. Invariant 11: a Buzz message is operational,
never a gate verdict. Everything in this module writes and reads *messages*;
nothing here can move a work package or record a review.

Reached over the relay's HTTP bridge rather than its CLI, because the CLI is one
of thirty-one Rust crates and building it would cost ten gigabytes on a host
with under forty free, to obtain a JSON-over-stdout wrapper around the same
three endpoints this file calls directly.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import uuid

from . import nostr

# One fixed namespace so a work package's channel id is the same on every run
# and on every relay. A lookup table would be one more piece of state that can
# be lost; a derivation cannot be.
DUME_CHANNEL_NAMESPACE = uuid.UUID("6f1d5b52-0d2e-5f6a-9c3b-4a7e8d1c2b3f")


# The ten message classes. A message declares which one it is; nothing reads it
# out of the prose. Kept here rather than imported from the contracts package so
# that the bridge stays importable without it — ADR-0001.
MESSAGE_TYPES = frozenset({
    "PROPOSAL", "CHALLENGE", "EVIDENCE", "REQUEST", "CORRECTION",
    "DISAGREEMENT", "CONSENSUS_CANDIDATE", "ABSTAIN", "STATUS", "BLOCKER"})

# Classes that are unanswerable without naming their subject. A challenge to
# nothing in particular cannot be closed, and an untethered claim of evidence is
# the shape a prohibited authority transfer takes when it is trying to look
# ordinary.
NEEDS_REFERENCE = frozenset({"CHALLENGE", "EVIDENCE", "CORRECTION",
                             "DISAGREEMENT", "CONSENSUS_CANDIDATE"})

# The tag a message declares its class in. Renamed with the product, and read
# under both names: 105 messages are already on the relay and six of them carry
# the old tag. Writing the new name and refusing the old one would make those
# six read as undeclared — and "nobody said" is a different fact from "somebody
# said STATUS", which is the whole reason the tag exists.
#
# The old names stay readable indefinitely. There is no migration to run and
# nothing to remember: the old form simply stops being written and fades as the
# messages carrying it age out.
TYPE_TAG = "aethrionis-type"
REF_TAG = "aethrionis-ref"
LEGACY_TYPE_TAGS = ("aethrionis-type", "aethrion-type")
LEGACY_REF_TAGS = ("aethrionis-ref", "aethrion-ref")


def declared_type(tags: list) -> str | None:
    """The class a message declared, under either name."""
    for tag in tags or []:
        if len(tag) > 1 and tag[0] in LEGACY_TYPE_TAGS:
            return tag[1]
    return None


def declared_refs(tags: list) -> list[str]:
    """What a message says it is about, under either name."""
    return [tag[1] for tag in tags or []
            if len(tag) > 1 and tag[0] in LEGACY_REF_TAGS and tag[1]]


def channel_id_for(wp_id: str) -> str:
    """The channel a work package talks in. Derived, not allocated.

    The derivation is deliberately unchanged and unprefixed: WP-001's channel
    already holds a commissioning run, and altering how the id is computed
    would point the name at a different, empty channel while leaving the
    messages somewhere nothing looks. A derived id is only useful while it
    keeps deriving the same thing.
    """
    return str(uuid.uuid5(DUME_CHANNEL_NAMESPACE, wp_id.upper()))


def channel_id(kind: str, name: str) -> str:
    """A channel id the relay will accept, derived from a name.

    The relay requires a lowercase UUID v4 — it advertises `h_grammar:
    uuid-v4-lowercase` in its NIP-11 document. A readable id like
    "dume-control" is accepted when the channel is *created* and then refused
    on every message sent to it, so the channel exists, looks fine, and is
    silently unusable. Deriving the id means the mapping lives in an algorithm
    rather than in a table that can drift, and the same name always resolves to
    the same channel after a restart.
    """
    return str(uuid.uuid5(DUME_CHANNEL_NAMESPACE, f"{kind}:{name}"))


# The eleven space channels, by the name a person types. Ids are derived, so
# this table names things rather than holding addresses.
SPACE_CHANNELS: dict[str, str] = {
    name: channel_id("SPACE", name) for name in (
        "dume-control", "dume-implementation", "dume-review",
        "dume-verification", "research-literature", "research-questions",
        "review-science", "decisions-escalations", "decisions-records",
        "operations-runtimes", "operations-incidents")
}

# Nostr event kinds Buzz assigns meaning to. Only the ones DUM-E actually uses.
KIND_TEXT_NOTE = 1
KIND_CHANNEL_MESSAGE = 9
KIND_GROUP_CREATE = 9007
KIND_METADATA = 0


class BuzzError(RuntimeError):
    """The relay refused, or could not be reached."""


class BuzzUnavailable(BuzzError):
    """The relay is not reachable.

    A distinct failure from a refusal. Invariant 16: a substrate outage is not
    an implementation failure, and the difference has to survive into the
    report or the wrong person gets asked to fix it.
    """


@dataclass
class Identity:
    """A role's presence on the substrate.

    Deliberately named for the role it serves, so a reader of the channel can
    tell who said what without consulting a mapping table.
    """
    name: str
    private_hex: str
    pubkey: str

    @classmethod
    def create(cls, name: str) -> "Identity":
        private_hex, pubkey = nostr.keypair()
        return cls(name=name, private_hex=private_hex, pubkey=pubkey)

    def public(self) -> dict:
        """What may be written down. The private half never appears."""
        return {"name": self.name, "pubkey": self.pubkey}


class BuzzClient:
    def __init__(self, base_url: str, identity: Identity, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.identity = identity
        self.timeout = timeout

    # ---- transport ------------------------------------------------------

    def _post(self, path: str, payload) -> dict:
        url = f"{self.base_url}{path}"
        body = json.dumps(payload, separators=(",", ":")).encode()
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header(
            "Authorization",
            nostr.nip98_header(self.identity.private_hex, url, "POST", body))
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:400]
            raise BuzzError(f"{path} refused with {exc.code}: {detail}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise BuzzUnavailable(f"{url} unreachable: {exc}") from None

    def relay_info(self) -> dict:
        """NIP-11 metadata. The cheapest proof the relay is actually there."""
        request = urllib.request.Request(self.base_url + "/")
        request.add_header("Accept", "application/nostr+json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode())
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise BuzzUnavailable(f"{self.base_url} unreachable: {exc}") from None

    # ---- events ---------------------------------------------------------

    def publish(self, kind: int, content: str, tags: list | None = None) -> dict:
        from ..secrets import assert_clean
        # A message goes to a substrate DUM-E does not own. Invariant 19 applies
        # here more than anywhere.
        assert_clean(content, where="Buzz message")
        event = nostr.sign_event(self.identity.private_hex, kind, content, tags or [])
        result = self._post("/events", event.as_dict())
        return {"event_id": event.id, "kind": kind, "accepted": result.get("accepted", True),
                "relay_response": result}

    def query(self, *, kinds: list[int] | None = None, authors: list[str] | None = None,
              tags: dict[str, list[str]] | None = None, limit: int = 50) -> list[dict]:
        filt: dict = {"limit": limit}
        if kinds:
            filt["kinds"] = kinds
        if authors:
            filt["authors"] = authors
        for name, values in (tags or {}).items():
            filt[f"#{name}"] = values
        # The bridge takes a bare array of filters, the same shape a Nostr REQ
        # carries, not an object wrapping them.
        result = self._post("/query", [filt])
        if isinstance(result, list):
            return result
        return result.get("events", [])

    # ---- membership -----------------------------------------------------

    def mint_invite(self, ttl_secs: int = 86400, max_uses: int = 50) -> str:
        """Owner-only: create an invite an agent identity can redeem.

        The relay is closed by design (`BUZZ_REQUIRE_RELAY_MEMBERSHIP=true`), so
        a freshly minted keypair cannot write until it has been let in. That is
        the correct default and DUM-E works with it rather than around it: an
        agent's ability to speak is granted deliberately and can be revoked.
        """
        result = self._post("/api/invites",
                            {"ttl_secs": ttl_secs, "max_uses": max_uses})
        code = result.get("code") or result.get("invite", {}).get("code")
        if not code:
            raise BuzzError(f"invite mint returned no code: {result}")
        return code

    def claim_invite(self, code: str) -> dict:
        """Redeem an invite as this client's identity, becoming a relay member."""
        return self._post("/api/invites/claim", {"code": code})

    def is_member(self) -> bool:
        """Can this identity actually write? Established by trying, not assumed."""
        try:
            self.query(kinds=[KIND_TEXT_NOTE], limit=1)
            return True
        except BuzzError:
            return False

    # ---- the operational surface DUM-E actually uses --------------------

    def create_channel(self, channel: str, name: str, about: str = "",
                       *, private: bool = True) -> dict:
        """Create a channel at an id we chose.

        The relay will allocate a UUID if none is given, but then the id exists
        only in its database and DUM-E would need a lookup table to find its own
        channel again after a restart. Supplying a derived id keeps the mapping
        in an algorithm instead of in a file that can go missing.

        `private` defaults to true because a work package's own channel is the
        cohort's working surface and has no reason to be in everyone's sidebar.
        DUM-E's four standing channels are the exception: they are where the
        harness reports to the operator, and a channel the operator cannot see
        is a report nobody reads. That was the state of it — a hundred messages
        in a channel the desktop had no idea existed.
        """
        return self.publish(KIND_GROUP_CREATE, "", [
            ["h", channel], ["name", name], ["about", about],
            ["private", "true" if private else "false"]])

    def announce(self, channel: str, text: str, mentions: list[str] | None = None,
                 *, message_type: str = "STATUS", refs: list[str] | None = None,
                 reply_to: str | None = None, thread_root: str | None = None
                 ) -> dict:
        """Say something in a channel, optionally waking named participants.

        A mention is a `p` tag. That is the whole wake mechanism, and it is why
        a role can be addressed by name without DUM-E owning a message bus.

        The declared type travels in a tag, never in the text. A reader that has
        to decide from prose whether a paragraph was a challenge or a status
        will sometimes decide wrong, and the seven prohibited authority
        transfers all begin with exactly that reading. The tag is a claim about
        what the sender meant, and nothing more: it still confers no authority,
        which is why EVIDENCE and CHALLENGE must also carry what they are about.
        """
        message_type = message_type.upper()
        if message_type not in MESSAGE_TYPES:
            raise BuzzError(
                f"{message_type!r} is not one of the ten message classes: "
                + ", ".join(sorted(MESSAGE_TYPES)))
        refs = list(refs or [])
        if message_type in NEEDS_REFERENCE and not refs:
            raise BuzzError(
                f"a {message_type} must name what it is about. Without a "
                "reference it cannot be answered, tracked or closed.")

        tags = [["h", channel], [TYPE_TAG, message_type]]
        for pubkey in mentions or []:
            tags.append(["p", pubkey])
        for ref in refs:
            tags.append([REF_TAG, ref])
        if thread_root:
            tags.append(["e", thread_root, "", "root"])
        if reply_to:
            tags.append(["e", reply_to, "", "reply"])
        return self.publish(KIND_CHANNEL_MESSAGE, text, tags)

    def read(self, channel: str, limit: int = 50) -> list[dict]:
        return self.query(kinds=[KIND_CHANNEL_MESSAGE], tags={"h": [channel]},
                          limit=limit)

    def set_profile(self, about: str = "") -> dict:
        return self.publish(KIND_METADATA, json.dumps(
            {"name": self.identity.name, "about": about}, separators=(",", ":")))


@dataclass
class Cohort:
    """The identities deployed for one work package.

    A deployment binding, not authority. Invariant 4: these are agent
    identities, and the role they serve is decided by DUM-E, not by anything
    Buzz records about them.
    """
    wp_id: str
    channel: str
    identities: dict[str, Identity] = field(default_factory=dict)
    created_at: str = ""

    def public(self) -> dict:
        return {"wp_id": self.wp_id, "channel": self.channel,
                "created_at": self.created_at,
                "identities": {role: ident.public()
                               for role, ident in self.identities.items()}}


def admit(owner: BuzzClient, identity: Identity, base_url: str) -> BuzzClient:
    """Let one identity onto the relay, and return a client bound to it.

    Two steps because they are two decisions: the owner chooses to admit, and
    the identity chooses to join. Collapsing them would mean the orchestrator
    could add members without an owner action, which is exactly the property a
    closed relay exists to have.
    """
    code = owner.mint_invite()
    member = BuzzClient(base_url, identity)
    member.claim_invite(code)
    return member


def deploy_cohort(client: BuzzClient, wp_id: str, roles: list[str]) -> Cohort:
    """Mint one identity per role and announce the package in its own channel.

    Buzz has no headless API for creating a *managed agent* or a *team* — those
    are authored in the desktop application. What it does have is exactly what
    is needed: a keypair is an identity, a channel is a UUID in an `h` tag, and
    a `p` tag wakes whoever it names. Building on those three facts keeps DUM-E
    out of a GUI it cannot run on a headless commissioning host.
    """
    channel = channel_id_for(wp_id)
    cohort = Cohort(wp_id=wp_id, channel=channel,
                    created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    # A repeated role — two implementers, a second verifier — is two agents, so
    # it must be two identities. Keying by role alone silently collapsed them
    # into one, which would have made "the second verifier" the same actor as
    # the first and quietly voided the independence the cohort was compiled for.
    for role in roles:
        key = role
        if key in cohort.identities:
            key = f"{role}#{sum(1 for k in cohort.identities if k.split('#')[0] == role) + 1}"
        cohort.identities[key] = Identity.create(f"{wp_id}/{key}")
    try:
        client.create_channel(channel, f"DUM-E {wp_id}",
                              "Commissioning channel. Operational only.")
    except BuzzError:
        # Already created by an earlier run. Deriving the id is what makes this
        # safe to re-run rather than something that needs a guard flag.
        pass
    client.announce(
        channel,
        f"{wp_id} commissioning channel opened. Roles: {', '.join(roles)}. "
        "Messages here are operational; no verdict recorded in this channel "
        "moves the package.",
        mentions=[i.pubkey for i in cohort.identities.values()])
    return cohort


def load_identity(path: Path | str, name: str = "dume_orchestrator") -> Identity:
    """Read an identity from the key store, which lives outside the repository."""
    path = Path(path)
    if not path.is_file():
        raise BuzzError(f"no identity store at {path}")
    data = json.loads(path.read_text())
    entry = data.get(name)
    if not entry:
        raise BuzzError(f"no identity named {name!r} in {path}")
    return Identity(name=name, private_hex=entry["private"], pubkey=entry["pubkey"])
