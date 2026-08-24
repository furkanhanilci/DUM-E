"""WP-003 — secrets, credentials and the local trust boundary.

Invariant 19: secrets never enter ordinary packets, Git or logs. That is a
control only if something actually inspects the payload before it is written,
so this module is what every packet builder, log writer and evidence recorder
calls, and it fails closed.

The detectors are deliberately shaped to catch the *carrier* — an assignment, a
header, a key block — rather than to guess at entropy, because a high-entropy
string is often a commit SHA or a model digest and those must pass freely.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

REDACTION = "«REDACTED:{kind}»"

# Ordered most specific first, so a token that matches two rules is named by the
# rule that identifies it precisely.
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private_key_block", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |PGP |DSA )?PRIVATE KEY-----.*?"
        r"-----END (?:RSA |EC |OPENSSH |PGP |DSA )?PRIVATE KEY-----",
        re.S)),
    ("anthropic_api_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai_api_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("huggingface_token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("telegram_bot_token", re.compile(r"\b\d{8,12}:AA[A-Za-z0-9_\-]{30,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("bearer_header", re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+\S{16,}")),
    ("url_with_credentials", re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s/:@]+:[^\s/@]{3,}@")),
    # Generic assignment: a name that means credential, followed by a value long
    # enough to be one.
    #
    # The name may carry a prefix, because the single most common way a
    # credential appears is as a SCREAMING_SNAKE environment variable —
    # POSTGRES_PASSWORD, BUZZ_S3_SECRET_KEY, GIT_HOOK_HMAC_SECRET. An earlier
    # version of this rule anchored on \b and matched none of them: "_" is a
    # word character, so there is no boundary before PASSWORD in
    # POSTGRES_PASSWORD. It reported a directory holding a private-key vault and
    # five live passwords as clean.
    ("credential_assignment", re.compile(
        r"(?i)(?:^|[^A-Za-z0-9_.\-])"          # not mid-identifier
        r"(?:[A-Za-z0-9_.\-]*?[_.\-])?"        # optional prefix ending at a separator
        r"(?:api[_.\-]?key|secret[_.\-]?key|private[_.\-]?key|privkey"
        # ACCESS_KEY is how S3 and every S3-compatible store names the first
        # half of its pair — AWS_ACCESS_KEY_ID, BUZZ_S3_ACCESS_KEY, MINIO_ROOT_
        # USER's sibling. The rule had access_token but not access_key, so it
        # read a live MinIO credential as ordinary configuration. Found by
        # scanning a deployment's own .env and checking what it did *not* say.
        r"|access[_.\-]?key"
        r"|access[_.\-]?token|auth[_.\-]?token|refresh[_.\-]?token"
        r"|client[_.\-]?secret|passphrase|password|passwd|credentials?"
        r"|secret|token|private|nsec)"
        # An optional `_ID` tail, because AWS_ACCESS_KEY_ID is how the most
        # widely copied credential name in existence is spelled. Deliberately
        # only `id` and not any suffix: a general tail would swallow
        # TOKEN_CACHE_DIR=/some/long/path, and a scanner that reports paths is
        # one whose findings get skimmed.
        r"(?:[_.\-]?id)?"
        r"[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_\-+/=]{12,})[\"']?")),
]

# Names that contain a credential word but denote the public half. Matching them
# would train the reader to ignore this scanner, which is how a scanner stops
# working.
PUBLIC_BY_NAME = re.compile(r"(?i)\b[A-Za-z0-9_.\-]*(?:public|pub)[_.\-]?key\b|pubkey")

# `TOKEN = generateToken()`, `secret = os.environ["X"]`, `key = other_key`. The
# right-hand side names where the value comes from; it is not the value. Code
# that derives a credential at run time is the correct thing to write, and a
# scanner that flags it teaches the reader that its findings are noise — which
# is how the one real finding gets waved through. Detected by looking at what
# follows the matched span rather than by widening the value pattern, because
# the value pattern is what keeps real secrets in.
DERIVED_VALUE = re.compile(r"\s*(?:\(|\.|\[|=>|\{)")

# Values that look like credentials but carry no secret. A scanner that cannot
# be satisfied by a correct configuration gets switched off, which is worse.
PLACEHOLDERS = re.compile(
    r"(?i)^(?:x{3,}|\*{3,}|\.{3,}|<[^>]+>|\$\{[^}]+\}|changeme\w*|placeholder\w*"
    r"|your[_\-]?\w+|dummy\w*|example\w*|redacted\w*|none|null|todo\w*"
    r"|«redacted[^»]*»)$")


@dataclass(frozen=True)
class Hit:
    kind: str
    start: int
    end: int
    preview: str

    def as_dict(self) -> dict:
        return {"kind": self.kind, "start": self.start, "end": self.end,
                "preview": self.preview}


class SecretLeak(RuntimeError):
    """A payload carrying a credential was refused before it could be written."""

    def __init__(self, hits: list[Hit], where: str = ""):
        self.hits = hits
        kinds = ", ".join(sorted({h.kind for h in hits}))
        suffix = f" in {where}" if where else ""
        super().__init__(f"refused: {len(hits)} credential(s) detected{suffix}: {kinds}")


def _is_placeholder(value: str) -> bool:
    return bool(PLACEHOLDERS.match(value.strip()))


def _preview(kind: str, matched: str) -> str:
    """Enough to find the secret and rotate it, never enough to use it."""
    body = matched.strip()
    if len(body) <= 8:
        return f"{kind}:<{len(body)} chars>"
    return f"{kind}:{body[:4]}…{body[-2:]} ({len(body)} chars)"


def scan(text: str) -> list[Hit]:
    """Every credential-shaped span in this text."""
    if not text:
        return []
    hits: list[Hit] = []
    claimed: list[tuple[int, int]] = []
    for kind, pattern in PATTERNS:
        for m in pattern.finditer(text):
            value = m.group(1) if m.groups() else m.group(0)
            if _is_placeholder(value):
                continue
            if kind == "credential_assignment" and PUBLIC_BY_NAME.search(m.group(0)):
                # A public key is published on purpose.
                continue
            if kind == "credential_assignment" and DERIVED_VALUE.match(
                    text[m.end(1):m.end(1) + 4]) and not m.group(0).rstrip().endswith(
                        ("'", '"')):
                # The value is an expression, not a literal.
                continue
            span = m.span()
            # A more specific rule already owns this span.
            if any(span[0] >= c[0] and span[1] <= c[1] for c in claimed):
                continue
            claimed.append(span)
            hits.append(Hit(kind, span[0], span[1], _preview(kind, value)))
    return sorted(hits, key=lambda h: h.start)


def redact(text: str) -> str:
    """Replace every detected credential with a marker that names its kind."""
    hits = scan(text)
    if not hits:
        return text
    out, cursor = [], 0
    for hit in hits:
        out.append(text[cursor:hit.start])
        out.append(REDACTION.format(kind=hit.kind))
        cursor = hit.end
    out.append(text[cursor:])
    return "".join(out)


def assert_clean(text: str, where: str = "") -> str:
    """Fail closed. Returns the text unchanged when it carries no credential."""
    hits = scan(text)
    if hits:
        raise SecretLeak(hits, where)
    return text


def scan_file(path: Path | str) -> list[Hit]:
    path = Path(path)
    try:
        return scan(path.read_text(errors="replace"))
    except (OSError, UnicodeDecodeError):
        return []


# Directories whose contents are reproduced from elsewhere. Scanning them
# reports the same finding twice and buries the one that matters.
SKIP_DIRS = (".git", "__pycache__", ".venv", "node_modules", ".pytest_cache",
             ".mypy_cache", ".ruff_cache", "dist", "build", ".tox")

ALLOWLIST_PATH = Path(__file__).resolve().parent.parent / "config" / "secret_scan_allowlist.json"


def load_allowlist(path: Path | None = None) -> list[dict]:
    """Known-synthetic credentials, each with a recorded reason.

    An allowlist entry is a reviewed decision, not a way to make a finding go
    away: an entry without a reason is refused, so silence always has an author.
    """
    import json
    path = Path(path) if path else ALLOWLIST_PATH
    if not path.is_file():
        return []
    entries = json.loads(path.read_text()).get("allow", [])
    for entry in entries:
        if not entry.get("reason"):
            raise ValueError(
                f"allowlist entry for {entry.get('path')!r} has no reason; "
                "an unexplained suppression is not a decision")
    return entries


def _allowed(entries: list[dict], rel_path: str, kind: str) -> dict | None:
    import fnmatch
    for entry in entries:
        if fnmatch.fnmatch(rel_path, entry["path"]) and (
                entry.get("kinds") in (None, "*") or kind in entry.get("kinds", [])):
            return entry
    return None


def scan_tree(root: Path | str, skip: tuple[str, ...] = SKIP_DIRS,
              allowlist: list[dict] | None = None
              ) -> dict[str, list[Hit]]:
    """Scan a directory, reporting only files that carry something."""
    root = Path(root)
    entries = load_allowlist() if allowlist is None else allowlist
    findings: dict[str, list[Hit]] = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if any(part in skip for part in path.parts):
            continue
        try:
            if path.stat().st_size > 4 * 1024 * 1024:
                continue
        except OSError:
            continue
        rel = str(path.relative_to(root))
        hits = [h for h in scan_file(path) if not _allowed(entries, rel, h.kind)]
        if hits:
            findings[rel] = hits
    return findings


def scan_tree_with_suppressions(root: Path | str, skip: tuple[str, ...] = SKIP_DIRS
                                ) -> dict:
    """Scan, and report suppressed hits separately so the count is never hidden."""
    root = Path(root)
    entries = load_allowlist()
    findings: dict[str, list[Hit]] = {}
    suppressed: list[dict] = []
    for path in Path(root).rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if any(part in skip for part in path.parts):
            continue
        try:
            if path.stat().st_size > 4 * 1024 * 1024:
                continue
        except OSError:
            continue
        rel = str(path.relative_to(root))
        live: list[Hit] = []
        for hit in scan_file(path):
            entry = _allowed(entries, rel, hit.kind)
            if entry:
                suppressed.append({"path": rel, "kind": hit.kind,
                                   "reason": entry["reason"]})
            else:
                live.append(hit)
        if live:
            findings[rel] = live
    return {"findings": findings, "suppressed": suppressed}
