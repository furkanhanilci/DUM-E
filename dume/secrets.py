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
    # enough to be one. Placeholders are excluded below rather than here, so the
    # rule stays readable.
    ("credential_assignment", re.compile(
        r"(?i)\b(?:api[_\-]?key|secret[_\-]?key|access[_\-]?token|auth[_\-]?token"
        r"|client[_\-]?secret|password|passwd|private[_\-]?token)\b"
        r"\s*[:=]\s*[\"']?([A-Za-z0-9_\-+/=]{12,})[\"']?")),
]

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


def scan_tree(root: Path | str, skip: tuple[str, ...] = (".git", "__pycache__",
                                                         ".venv", "node_modules")
              ) -> dict[str, list[Hit]]:
    """Scan a directory, reporting only files that carry something."""
    root = Path(root)
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
        hits = scan_file(path)
        if hits:
            findings[str(path.relative_to(root))] = hits
    return findings
