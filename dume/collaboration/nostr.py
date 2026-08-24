"""The minimum Nostr an orchestrator needs: sign an event, sign a request.

Buzz's whole surface is signed Nostr events, and its HTTP bridge authenticates
with NIP-98. Both are small and exactly specified, so DUM-E implements them
directly rather than building a 31-crate Rust workspace to reach them — the
harness needs the wire format, not the client.

What is deliberately *not* here: NIP-44 encryption, subscriptions, and the
managed-agent lifecycle. Those either are not needed yet or genuinely live
elsewhere, and pretending otherwise would grow DUM-E into the thing it is
supposed to commission.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import json
import time
from dataclasses import dataclass, field

import coincurve

HTTP_AUTH_KIND = 27235


def keypair() -> tuple[str, str]:
    """A fresh identity: (private hex, x-only public hex)."""
    key = coincurve.PrivateKey()
    return key.secret.hex(), key.public_key.format(compressed=True)[1:].hex()


def pubkey_of(private_hex: str) -> str:
    key = coincurve.PrivateKey(bytes.fromhex(private_hex))
    return key.public_key.format(compressed=True)[1:].hex()


def _serialise(pubkey: str, created_at: int, kind: int, tags: list, content: str) -> bytes:
    """The canonical form an event id is taken over.

    Separators matter: the digest is over a compact array with no incidental
    whitespace, and a space anywhere in it produces a different id and a
    signature nothing will accept.
    """
    return json.dumps([0, pubkey, created_at, kind, tags, content],
                      separators=(",", ":"), ensure_ascii=False).encode()


@dataclass
class Event:
    kind: int
    content: str = ""
    tags: list = field(default_factory=list)
    created_at: int = 0
    pubkey: str = ""
    id: str = ""
    sig: str = ""

    def as_dict(self) -> dict:
        return {"id": self.id, "pubkey": self.pubkey, "created_at": self.created_at,
                "kind": self.kind, "tags": self.tags, "content": self.content,
                "sig": self.sig}


def sign_event(private_hex: str, kind: int, content: str = "",
               tags: list | None = None, created_at: int | None = None) -> Event:
    key = coincurve.PrivateKey(bytes.fromhex(private_hex))
    pubkey = key.public_key.format(compressed=True)[1:].hex()
    tags = tags or []
    created_at = created_at if created_at is not None else int(time.time())
    digest = hashlib.sha256(
        _serialise(pubkey, created_at, kind, tags, content)).digest()
    return Event(kind=kind, content=content, tags=tags, created_at=created_at,
                 pubkey=pubkey, id=digest.hex(),
                 sig=key.sign_schnorr(digest).hex())


def verify_event(event: dict) -> bool:
    """Recompute the id and check the signature. Used to test our own signer."""
    try:
        digest = hashlib.sha256(_serialise(
            event["pubkey"], event["created_at"], event["kind"],
            event["tags"], event["content"])).digest()
        if digest.hex() != event["id"]:
            return False
        pub = coincurve.PublicKeyXOnly(bytes.fromhex(event["pubkey"]))
        return pub.verify(bytes.fromhex(event["sig"]), digest)
    except (KeyError, ValueError, TypeError):
        return False


def nip98_header(private_hex: str, url: str, method: str,
                 body: bytes | None = None) -> str:
    """An `Authorization: Nostr …` value for one request.

    The signature binds the URL, the method and — when a body is present — its
    digest, so the header cannot be lifted onto a different request. The relay
    also enforces a ±60s window and rejects a replayed event id, which is why
    these are minted per call rather than cached.
    """
    # A nonce, because the event id is a hash of everything else in it and
    # nothing else varies: two identical POSTs to the same URL inside the same
    # second produce the same id, and the relay correctly calls the second one
    # a replay. Minting six invites in a loop failed on the second.
    tags = [["u", url], ["method", method.upper()],
            ["nonce", secrets.token_hex(16)]]
    if body is not None:
        tags.append(["payload", hashlib.sha256(body).hexdigest()])
    event = sign_event(private_hex, HTTP_AUTH_KIND, "", tags)
    blob = json.dumps(event.as_dict(), separators=(",", ":")).encode()
    return "Nostr " + base64.b64encode(blob).decode()
