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


def test_an_allowlist_entry_without_a_reason_is_refused(tmp_path):
    """A suppression must always have an author. Silence with no reason is not
    a decision, it is a leak waiting to be blamed on nobody."""
    import json
    path = tmp_path / "allow.json"
    path.write_text(json.dumps({"allow": [{"path": "x.py", "kinds": "*"}]}))
    with pytest.raises(ValueError, match="no reason"):
        secrets.load_allowlist(path)


def test_suppressed_hits_are_reported_rather_than_hidden(tmp_path):
    (tmp_path / "fixture.py").write_text(f"KEY = '{REAL['github_token']}'\n")
    (tmp_path / "real.py").write_text(f"KEY = '{REAL['anthropic_api_key']}'\n")
    allow = [{"path": "fixture.py", "kinds": "*", "reason": "test fixture"}]
    import dume.secrets as m
    original = m.load_allowlist
    m.load_allowlist = lambda path=None: allow
    try:
        report = m.scan_tree_with_suppressions(tmp_path)
    finally:
        m.load_allowlist = original
    assert set(report["findings"]) == {"real.py"}
    assert [s["path"] for s in report["suppressed"]] == ["fixture.py"]
    assert report["suppressed"][0]["reason"] == "test fixture"


def test_transient_tool_caches_are_not_scanned(tmp_path):
    """A cache reproduces findings from elsewhere and buries the one that matters."""
    cache = tmp_path / ".pytest_cache" / "v"
    cache.mkdir(parents=True)
    (cache / "nodeids").write_text(REAL["github_token"])
    assert secrets.scan_tree(tmp_path, allowlist=[]) == {}


def test_the_repository_itself_carries_no_unsuppressed_credential():
    """The control, applied to the thing it protects."""
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    report = secrets.scan_tree_with_suppressions(repo)
    assert report["findings"] == {}, report["findings"]


def test_evidence_is_redacted_at_the_moment_it_becomes_a_file(tmp_path):
    """I-19 applied at the write, not delegated to every caller's memory."""
    from dume.state import json_dump
    out = tmp_path / "receipt.json"
    json_dump({"note": f"observed {REAL['github_token']} in the payload"}, out)
    body = out.read_text()
    assert REAL["github_token"] not in body
    assert "REDACTED:github_token" in body


# The shapes that a real deployment actually produces. An earlier version of the
# detector reported a directory holding a private-key vault and five live
# passwords as clean, because it anchored on \b and "_" is a word character —
# so there is no boundary before PASSWORD in POSTGRES_PASSWORD.
ENVIRONMENT_SHAPES = [
    ("POSTGRES_PASSWORD", "POSTGRES_PASSWORD=aB3xZ9qW7eR1tY2uI4oP"),
    ("screaming snake private key", "BUZZ_RELAY_PRIVATE_KEY=" + "a1b2c3d4" * 8),
    ("suffix secret", "BUZZ_GIT_HOOK_HMAC_SECRET=" + "f" * 64),
    ("nested secret key", "BUZZ_S3_SECRET_KEY=abcdefghij123456"),
    ("json private field", '  "private": "' + "9" * 64 + '",'),
    ("lowercase yaml", "  api_key: aB3xZ9qW7eR1tY2uI4oP"),
    ("dotted", "db.password = aB3xZ9qW7eR1tY2uI4oP"),
]

PUBLIC_OR_INNOCENT = [
    ("owner pubkey", "RELAY_OWNER_PUBKEY=" + "9f907217" * 8),
    ("ssh public key", "SSH_PUBLIC_KEY=AAAAB3NzaC1yc2EAAAADAQABAAAB"),
    ("commit sha", "candidate_revision: c750946d0ee08e58e3090f979630743aafcf9696"),
    ("artefact digest", "artefact_sha256: " + "e3b0c442" * 8),
    ("schema name", '"schema": "dume.secret_scan_allowlist/1"'),
    ("boolean", "private_repo: true"),
    ("placeholder", "api_key = changeme"),
]


@pytest.mark.parametrize("label,line", ENVIRONMENT_SHAPES)
def test_deployment_credential_shapes_are_caught(label, line):
    assert secrets.scan(line), f"{label} was missed"


@pytest.mark.parametrize("label,line", PUBLIC_OR_INNOCENT)
def test_public_and_innocent_values_are_not_flagged(label, line):
    """A scanner that cannot be satisfied by a correct configuration gets
    switched off, and then it protects nothing."""
    assert secrets.scan(line) == [], f"{label} was a false positive"


def test_a_generated_env_file_is_not_reported_as_clean(tmp_path):
    """The regression that mattered: a whole file of live secrets, missed."""
    env = tmp_path / "relay.env"
    env.write_text("\n".join([
        "# generated deployment environment",
        "BUZZ_IMAGE=ghcr.io/block/buzz:sha-0720f53",
        "RELAY_OWNER_PUBKEY=" + "9f907217" * 8,
        "BUZZ_RELAY_PRIVATE_KEY=" + "a1b2c3d4" * 8,
        "POSTGRES_PASSWORD=aB3xZ9qW7eR1tY2uI4oP",
        "REDIS_PASSWORD=Xy7-abcdEFGH1234_ijkl",
        "BUZZ_S3_SECRET_KEY=abcdefghij123456",
    ]) + "\n")
    hits = secrets.scan_file(env)
    assert len(hits) >= 4, [h.preview for h in hits]
    # ...and the public half is still not among them.
    assert all("9f907217" not in h.preview for h in hits)


def test_a_credential_derived_at_run_time_is_not_a_credential():
    """`TOKEN = generateToken()` names where a value comes from, not the value.

    Regression: the vendored Superpowers brainstorming server does exactly this,
    and the scanner reported the repository dirty over it. A scanner that flags
    correct code teaches the reader to skim its findings, which is how the one
    real finding gets waved through.
    """
    from dume.secrets import scan
    derived = [
        "TOKEN = generateToken();",
        'secret = os.environ["BUZZ_SECRET_KEY"]',
        "const apiKey = buildKey.from(seed)",
        "password = getPassword()",
        "auth_token = tokens[0]",
        "private_key = load_key => decode(raw)",
    ]
    for line in derived:
        assert scan(line) == [], f"derived value reported as a credential: {line}"

    literal = [
        "POSTGRES_PASSWORD=hunter2hunter2hunter2",
        'api_key = "sk-abcdef0123456789"',
        "client_secret: 8f3aa91c77bd4e0192aa",
        "BUZZ_RELAY_PRIVATE_KEY=5c260721ad0a733c8c02eaaca38bb576",
    ]
    for line in literal:
        assert scan(line), f"a real credential stopped being detected: {line}"


def test_an_access_key_is_a_credential():
    """S3 and everything S3-compatible names half its pair ACCESS_KEY.

    Regression: the rule matched `access_token` but not `access_key`, so a live
    MinIO credential in a deployment's own .env read as ordinary configuration.
    The value is not the secret half, but it names an account, and a scanner
    that stays silent about it teaches the reader that .env files are clean.
    """
    from dume.secrets import scan
    for line in (
        "BUZZ_S3_ACCESS_KEY=3d21a6ef0be730354eead826",
        "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLEX",
        "access-key: 8f3aa91c77bd4e0192aa",
    ):
        assert scan(line), f"an access key went unreported: {line}"

    # The public half stays public: a pubkey is published on purpose, and
    # flagging it is how a scanner trains its reader to skim.
    assert scan("RELAY_OWNER_PUBKEY=3f25f7cd72f4ca3472a1936569523f9ae9b15791") == []


def test_a_credential_name_may_end_in_id():
    """AWS_ACCESS_KEY_ID is the most widely copied credential name there is.

    The rule required the credential word to sit immediately before the `=`, so
    a trailing `_ID` hid it. Only `id` is allowed as a tail: a general suffix
    would swallow TOKEN_CACHE_DIR=/some/long/path, and a scanner that reports
    paths is one whose findings get skimmed.
    """
    from dume.secrets import scan
    assert scan("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLEX")
    assert scan("client_secret_id: 8f3aa91c77bd4e0192aa")
    assert scan("TOKEN_CACHE_DIR=/home/otonom/some/long/path") == []
