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


def channel_id_for(wp_id: str) -> str:
    """The channel a work package talks in. Derived, not allocated."""
    return str(uuid.uuid5(DUME_CHANNEL_NAMESPACE, wp_id.upper()))

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

    def create_channel(self, channel: str, name: str, about: str = "") -> dict:
        """Create a channel at an id we chose.

        The relay will allocate a UUID if none is given, but then the id exists
        only in its database and DUM-E would need a lookup table to find its own
        channel again after a restart. Supplying a derived id keeps the mapping
        in an algorithm instead of in a file that can go missing.
        """
        return self.publish(KIND_GROUP_CREATE, "", [
            ["h", channel], ["name", name], ["about", about], ["private", "true"]])

    def announce(self, channel: str, text: str, mentions: list[str] | None = None
                 ) -> dict:
        """Say something in a channel, optionally waking named participants.

        A mention is a `p` tag. That is the whole wake mechanism, and it is why
        a role can be addressed by name without DUM-E owning a message bus.
        """
        tags = [["h", channel]]
        for pubkey in mentions or []:
            tags.append(["p", pubkey])
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
