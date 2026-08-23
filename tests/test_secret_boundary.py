"""WP-003. A credential must not be able to reach a packet, a log or Git."""
import pytest

from dume import secrets


REAL = {
    "anthropic_api_key": "sk-ant-api03-" + "Ab3Xz9" * 8,
    "github_token": "ghp_" + "aB3xZ9qW7e" * 3 + "12",
    "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
    "huggingface_token": "hf_" + "qWeRtY1234" * 3 + "zz",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "telegram_bot_token": "123456789:AA" + "Ff1234567890abcdefGHIJKLMNOPqrst",
}


@pytest.mark.parametrize("kind,value", sorted(REAL.items()))
def test_each_credential_shape_is_detected(kind, value):
    hits = secrets.scan(f"token = {value}")
    assert hits, f"{kind} was not detected"
    assert kind in {h.kind for h in hits}


def test_assert_clean_fails_closed(tmp_path):
    payload = f"WP packet\nANTHROPIC_API_KEY={REAL['anthropic_api_key']}\n"
    with pytest.raises(secrets.SecretLeak):
        secrets.assert_clean(payload, where="WP-029 packet")


def test_clean_payload_passes_through_unchanged():
    payload = "wp_id: WP-001\ncandidate_revision: 5a00264159ba7c3e21ce8557cbe1524513\n"
    assert secrets.assert_clean(payload) == payload


def test_commit_sha_and_model_digest_are_not_treated_as_secrets():
    """A scanner that flags every high-entropy string gets switched off."""
    payload = (
        "candidate_revision: c750946d0ee08e58e3090f979630743aafcf9696\n"
        "artefact_sha256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n"
        "model_digest: sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08\n")
    assert secrets.scan(payload) == []


def test_placeholders_are_not_secrets():
    for value in ("changeme", "<your-token-here>", "${API_KEY}", "xxxxxxxxxxxx",
                  "placeholder", "REDACTED"):
        assert secrets.scan(f"api_key = {value}") == [], value


def test_redaction_removes_the_secret_but_names_its_kind():
    payload = f"Authorization: Bearer {REAL['github_token']}"
    out = secrets.redact(payload)
    assert REAL["github_token"] not in out
    assert "REDACTED" in out


def test_preview_never_reproduces_the_whole_secret():
    """A finding must be actionable without becoming a second copy of the leak."""
    value = REAL["anthropic_api_key"]
    hit = secrets.scan(f"key={value}")[0]
    assert value not in hit.preview
    assert len(hit.preview) < len(value)


def test_private_key_block_is_detected():
    blob = ("-----BEGIN OPENSSH PRIVATE KEY-----\n"
            "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB\n"
            "-----END OPENSSH PRIVATE KEY-----")
    assert {h.kind for h in secrets.scan(blob)} == {"private_key_block"}


def test_credentials_in_a_url_are_detected():
    assert secrets.scan("postgres://svc:s3cr3tpassword@db.local/x")


def test_scan_tree_finds_a_planted_credential(tmp_path):
    (tmp_path / "ok.md").write_text("nothing here\n")
    (tmp_path / "leak.env").write_text(f"GITHUB_TOKEN={REAL['github_token']}\n")
    findings = secrets.scan_tree(tmp_path)
    assert set(findings) == {"leak.env"}
