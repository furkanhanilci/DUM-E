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

# The tag a message declares its class in, and the tag naming what it is about.
# These are the only names read or written. Messages posted to the relay under a
# tag this harness no longer knows read as *undeclared* — "nobody said" rather
# than "somebody said STATUS" — which is a real difference, so a tag rename is a
# change to make deliberately and not as a side effect.
TYPE_TAG = "dume-type"
REF_TAG = "dume-ref"


def declared_type(tags: list) -> str | None:
    """The class a message declared, if it declared one."""
    for tag in tags or []:
        if len(tag) > 1 and tag[0] == TYPE_TAG:
            return tag[1]
    return None


def declared_refs(tags: list) -> list[str]:
    """What a message says it is about."""
    return [tag[1] for tag in tags or []
            if len(tag) > 1 and tag[0] == REF_TAG and tag[1]]


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
# What each space is called in the sidebar. The id is derived from the slug,
# so the title is free to read like a name rather than a key.
SPACE_TITLES: dict[str, str] = {
    "dume-control": "DUM-E · control",
    "dume-implementation": "DUM-E · implementation",
    "dume-review": "DUM-E · review",
    "dume-verification": "DUM-E · verification",
    "research-literature": "Research · literature",
    "research-questions": "Research · questions",
    "review-science": "Review · science",
    "decisions-escalations": "Decisions · escalations",
    "decisions-records": "Decisions · records",
    "operations-runtimes": "Operations · runtimes",
    "operations-incidents": "Operations · incidents",
}

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
KIND_GROUP_ADD_MEMBER = 9000
KIND_AGENT_DIRECTORY = 10100
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

    def add_member(self, channel: str, pubkey: str, role: str = "member") -> dict:
        """Put someone into a channel.

        Creating a channel does not put anyone in it, not even a reader who is
        allowed to see it. The desktop's sidebar lists channels you belong to,
        so a channel nobody was added to is one nobody has a way to open —
        which is how DUM-E reported for a hundred messages into a room the
        operator could not find.
        """
        # The role travels in its own tag. Put inside the `p` tag it is simply
        # ignored, and the relay records the default — which is how six agents
        # were seated as ordinary members and never appeared under Agents,
        # where only the bot role is looked up.
        return self.publish(KIND_GROUP_ADD_MEMBER, "",
                            [["h", channel], ["p", pubkey], ["role", role]])

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

    def announce_agent(self, name: str, about: str, channels: list[str],
                       capabilities: list[str] | None = None) -> dict:
        """List this identity in the desktop's Agents directory.

        A profile makes a key show a name in a channel; it does not make it an
        agent. The desktop builds that list from a self-signed directory record
        and keeps only members the relay has recorded in the bot role — two
        facts that must both hold, which is why six named, seated roles still
        appeared nowhere under Agents.

        Nothing here grants authority. It is a description of what this
        identity is for, published by the identity itself, and the harness
        remains the only thing that decides what any of them may do.
        """
        return self.publish(KIND_AGENT_DIRECTORY, json.dumps({
            "name": name,
            "display_name": name,
            "about": about,
            "agent_type": "agent",
            "channels": channels,
            "channel_ids": channels,
            "capabilities": capabilities or [],
            "status": "online",
        }, separators=(",", ":")))

    def set_profile(self, about: str = "", name: str | None = None) -> dict:
        """Publish who this key is.

        Without it a channel shows a hex string where a name belongs, and six
        roles are six indistinguishable strangers.
        """
        return self.publish(KIND_METADATA, json.dumps(
            {"name": name or self.identity.name, "about": about},
            separators=(",", ":")))


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
    # Which identities could not be admitted, named or seated. Kept rather than
    # raised: a substrate problem is not an implementation problem, and a run
    # that stops because a profile failed to publish has stopped for the wrong
    # reason. Recorded so the silence that produced an anonymous cohort is not
    # what happens next time either.
    faults: list[str] = field(default_factory=list)

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


def ensure_spaces(client: BuzzClient, operator: str | None = None) -> dict[str, str]:
    """Create the eleven standing channels and put the operator in them.

    Two separate things have to be true before a report is readable, and each
    was missed once. The channel must be public, or the desktop never lists it;
    and the operator must be a member, or it is not in their sidebar even so. A
    channel that is public but memberless looks identical, from the harness's
    side, to one that works.

    Re-running is the normal case: a channel that exists refuses creation, and
    adding a member who is already one is not an error.
    """
    outcome: dict[str, str] = {}
    for name, channel in SPACE_CHANNELS.items():
        try:
            client.create_channel(channel, SPACE_TITLES.get(name, name),
                                  "A standing DUM-E workspace space.", private=False)
            # The relay accepts a create for an id that already exists, so
            # this says what was asserted, not what was new. Claiming
            # "created" for eleven channels that were already there reads as
            # a fresh deployment every time health runs.
            outcome[name] = "asserted"
        except BuzzError:
            outcome[name] = "already there"
        if operator:
            try:
                client.add_member(channel, operator)
            except BuzzError as exc:
                outcome[name] += f", but the operator was not added: {exc}"
    return outcome


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

    # Each identity is admitted, named and seated before it says anything.
    # Without this a cohort was six unnamed keys: the relay showed hex strings
    # where participants belong, and a reader could not tell the implementer
    # from the reviewer who refused its candidate. Failures here are not fatal
    # — the run has nothing wrong with it — but they are visible rather than
    # silent, because an anonymous cohort is what silence produced before.
    for key, identity in cohort.identities.items():
        role = key.split("#")[0]
        try:
            speaker = BuzzClient(client.base_url, identity, client.timeout)
            if not speaker.is_member():
                speaker = admit(client, identity, client.base_url)
            speaker.set_profile(
                name=f"{role.replace('_', ' ')} · {wp_id}",
                about=ROLE_ABOUT.get(role, f"A DUM-E {role}.")
                      + f" Deployed for {wp_id}.")
            client.add_member(channel, identity.pubkey, role="bot")
        except BuzzError as exc:
            cohort.faults.append(f"{key}: {exc}")
    client.announce(
        channel,
        f"{wp_id} commissioning channel opened. Roles: {', '.join(roles)}. "
        "Messages here are operational; no verdict recorded in this channel "
        "moves the package.",
        mentions=[i.pubkey for i in cohort.identities.values()])
    return cohort


# Which of DUM-E's standing channels each role belongs in. A role that never
# speaks in a space has no business appearing in its member list.
ROLE_CHANNELS: dict[str, tuple[str, ...]] = {
    "commissioning_orchestrator": ("dume-control", "dume-implementation",
                                   "dume-review", "dume-verification"),
    "architect": ("dume-control", "dume-implementation"),
    "implementer": ("dume-implementation",),
    "spec_reviewer": ("dume-review",),
    "code_reviewer": ("dume-review",),
    "verifier": ("dume-verification",),
}

ROLE_ABOUT: dict[str, str] = {
    "commissioning_orchestrator": "Runs the commissioning pipeline. Records "
                                  "what happened; decides nothing on its own.",
    "architect": "Turns a frozen packet into a plan. Does not implement it and "
                 "does not judge it afterwards.",
    "implementer": "Writes the failing test, then the code that passes it. "
                   "Never reviews its own candidate.",
    "spec_reviewer": "Asks only whether the candidate does what the packet "
                     "said. Independent of whoever wrote it.",
    "code_reviewer": "Asks only whether the candidate is sound as code.",
    "verifier": "Runs the evidence from a fresh checkout. The only role whose "
                "answer comes from execution rather than reading.",
}


def role_identity(role: str, path: Path | str) -> Identity:
    """The identity a role speaks with, stable across runs.

    `Identity.create` mints a fresh keypair every time it is called, so every
    run introduced six strangers into the channels: the architect who planned
    yesterday's package and the one who planned today's had no visible relation
    to each other. A reader cannot follow a conversation between participants
    who are new each time.

    Persisted rather than derived from a seed, because a derived key is only as
    stable as the derivation, and changing it later would silently orphan every
    message the old key signed.
    """
    path = Path(path)
    data = json.loads(path.read_text()) if path.is_file() else {}
    key = f"role:{role}"
    entry = data.get(key)
    if entry:
        return Identity(name=role, private_hex=entry["private"],
                        pubkey=entry["pubkey"])
    identity = Identity.create(role)
    data[key] = {"private": identity.private_hex, "pubkey": identity.pubkey}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    path.chmod(0o600)
    return identity


def ensure_roles(client: BuzzClient, path: Path | str,
                 roles: tuple[str, ...] = tuple(ROLE_CHANNELS)) -> dict[str, str]:
    """Give each role a name, a description and a seat in its channels.

    Three separate things, each of which was missing. Without a profile the
    channel shows a hex string; without membership the role is not in the
    participant list even though it posts there; and without both, four
    channels with six participants looked like four channels with one.
    """
    out: dict[str, str] = {}

    # DUM-E's own key speaks in every channel and had no profile at all, so the
    # harness itself appeared as a hex string next to six named roles. It is
    # the one participant a reader is most likely to be looking for.
    try:
        harness = load_identity(path, "dume_orchestrator")
        speaker = BuzzClient(client.base_url, harness, client.timeout)
        if not speaker.is_member():
            speaker = admit(client, harness, client.base_url)
        about = ("The commissioning harness. Records what happened; the gate "
                 "decides, and nothing said in a channel moves a package.")
        speaker.set_profile(name="DUM-E", about=about)
        seats = ROLE_CHANNELS["commissioning_orchestrator"]
        for name in seats:
            client.add_member(SPACE_CHANNELS[name], harness.pubkey, role="bot")
        speaker.announce_agent(
            "DUM-E", about, [SPACE_CHANNELS[n] for n in seats],
            ["commission", "record", "report"])
        out["dume"] = harness.pubkey[:12]
    except BuzzError as exc:
        out["dume"] = f"not named: {exc}"

    for role in roles:
        identity = role_identity(role, path)
        speaker = BuzzClient(client.base_url, identity, client.timeout)
        # A key that is not a relay member cannot publish at all, so the
        # profile — the first thing a role does — is refused before anything
        # else is tried. Admission first, and it is the owner's decision.
        if not speaker.is_member():
            speaker = admit(client, identity, client.base_url)
        about = ROLE_ABOUT.get(role, f"A DUM-E {role}.")
        speaker.set_profile(name=role.replace("_", " "), about=about)
        speaker.announce_agent(
            role.replace("_", " "), about,
            [SPACE_CHANNELS[n] for n in ROLE_CHANNELS.get(role, ())],
            [role])
        for name in ROLE_CHANNELS.get(role, ()):
            try:
                # Added by the owner: a role cannot put itself in a room.
                client.add_member(SPACE_CHANNELS[name], identity.pubkey,
                              role="bot")
            except BuzzError as exc:
                out[role] = f"not seated in {name}: {exc}"
                break
        else:
            out[role] = identity.pubkey[:12]
    return out


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
