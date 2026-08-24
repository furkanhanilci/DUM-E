"""Nostr signing, and the boundary Buzz is not allowed to cross.

The live-relay tests skip when no relay is running, because a test that needs
infrastructure should say so rather than fail as though the code were wrong.
"""
import json
import urllib.error
import urllib.request

import pytest

from dume.collaboration import nostr
from dume.collaboration.buzz import (BuzzClient, BuzzError, BuzzUnavailable,
                                     Cohort, Identity, channel_id_for)

RELAY = "http://127.0.0.1:3000"


def _relay_up() -> bool:
    try:
        urllib.request.urlopen(RELAY + "/", timeout=2).read(1)
        return True
    except (urllib.error.URLError, OSError):
        return False


needs_relay = pytest.mark.skipif(not _relay_up(), reason="no Buzz relay on :3000")


# ---- signing ------------------------------------------------------------

def test_a_signed_event_verifies():
    private, _ = nostr.keypair()
    event = nostr.sign_event(private, 1, "hello", [["t", "commissioning"]])
    assert nostr.verify_event(event.as_dict())


@pytest.mark.parametrize("field,value", [
    ("content", "tampered"), ("kind", 7), ("created_at", 1),
])
def test_tampering_with_any_signed_field_breaks_verification(field, value):
    private, _ = nostr.keypair()
    event = nostr.sign_event(private, 1, "hello").as_dict()
    event[field] = value
    assert not nostr.verify_event(event)


def test_the_event_id_is_over_a_compact_serialisation():
    """A stray space produces a different id and a signature nothing accepts."""
    private, pub = nostr.keypair()
    event = nostr.sign_event(private, 1, "x", [], created_at=1700000000)
    import hashlib
    expected = hashlib.sha256(json.dumps(
        [0, pub, 1700000000, 1, [], "x"],
        separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert event.id == expected


def test_a_nip98_header_binds_url_method_and_body():
    import base64
    private, _ = nostr.keypair()
    header = nostr.nip98_header(private, "http://h/query", "POST", b'{"a":1}')
    event = json.loads(base64.b64decode(header.split(" ", 1)[1]))
    assert event["kind"] == nostr.HTTP_AUTH_KIND
    tags = {t[0]: t[1] for t in event["tags"]}
    assert tags["u"] == "http://h/query"
    assert tags["method"] == "POST"
    import hashlib
    assert tags["payload"] == hashlib.sha256(b'{"a":1}').hexdigest()
    assert nostr.verify_event(event)


def test_two_headers_for_the_same_request_are_different_events():
    """The relay rejects a replayed event id, so headers are minted per call."""
    private, _ = nostr.keypair()
    a = nostr.nip98_header(private, "http://h/x", "POST")
    b = nostr.nip98_header(private, "http://h/x", "POST")
    assert a != b


# ---- the authority boundary --------------------------------------------

def test_the_collaboration_layer_cannot_move_a_work_package():
    """Invariant 11. If this module ever imports the state store, a message has
    become able to decide something."""
    import inspect

    from dume.collaboration import buzz
    source = inspect.getsource(buzz)
    for forbidden in ("from ..state", "import state", "record_review",
                      "transition(", "MergeGate"):
        assert forbidden not in source, f"collaboration reached for {forbidden}"


def test_a_credential_cannot_be_published(monkeypatch):
    """A message goes to a substrate DUM-E does not own."""
    from dume.secrets import SecretLeak
    client = BuzzClient(RELAY, Identity.create("test"))
    monkeypatch.setattr(client, "_post", lambda *a, **k: pytest.fail(
        "the credential reached the transport"))
    with pytest.raises(SecretLeak):
        client.publish(1, "here is the key: ghp_" + "aB3xZ9qW7e" * 3 + "12")


def test_an_unreachable_relay_is_a_distinct_failure():
    """Invariant 16: a substrate outage is not an implementation failure."""
    client = BuzzClient("http://127.0.0.1:1", Identity.create("test"))
    with pytest.raises(BuzzUnavailable):
        client.relay_info()
    assert issubclass(BuzzUnavailable, BuzzError)


def test_an_identity_never_serialises_its_private_half():
    identity = Identity.create("WP-001/verifier")
    assert "private" not in json.dumps(identity.public())
    assert identity.private_hex not in json.dumps(identity.public())
    cohort = Cohort("WP-001", "chan", {"verifier": identity})
    assert identity.private_hex not in json.dumps(cohort.public())


# ---- channel identity ---------------------------------------------------

def test_a_channel_id_is_derived_and_stable():
    """A derivation cannot be lost; a lookup table can."""
    assert channel_id_for("WP-035") == channel_id_for("wp-035")
    assert channel_id_for("WP-035") != channel_id_for("WP-036")
    import uuid
    uuid.UUID(channel_id_for("WP-001"))  # raises if not a UUID


# ---- against the live relay --------------------------------------------

@needs_relay
def test_the_relay_answers_with_its_capabilities():
    client = BuzzClient(RELAY, Identity.create("probe"))
    info = client.relay_info()
    assert info["name"]
    assert 1 in info["supported_nips"]


@needs_relay
def test_an_unadmitted_identity_cannot_write():
    """The relay is closed by design; a fresh keypair is not a member."""
    client = BuzzClient(RELAY, Identity.create("stranger"))
    with pytest.raises(BuzzError, match="membership|403"):
        client.publish(1, "let me in")
