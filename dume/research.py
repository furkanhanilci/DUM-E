"""The research area — where an integration decision leaves a trail.

The commissioning plan requires fresh research before every integration: fetch
the upstream, record the revision, read the source for the exact capability
being relied on, search open issues for it, check the licence, and compare what
was found against what the plan assumed. Then, if the evidence warrants it,
change the plan through a recorded decision rather than quietly doing something
else.

That process produces three kinds of record, and this module owns all three:

* a **research log** entry — what was asked, where it was looked for, what was
  found, and what could not be established
* an **adoption record** — a mechanism classified as DEPENDENCY, ADAPTER,
  DIRECT_ADAPT, STANDARD, OPTIONAL_BACKEND, PATTERN, DEFER or REJECTED, with the
  licence and the pinned revision that classification rests on
* a **DUME-ADR** — the decision itself, what it supersedes, and the evidence

The rule these enforce together: a finding that changes nothing is a note, and a
decision with no finding behind it is a preference. Neither is allowed to look
like the other.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RESEARCH_DIR = REPO / "docs" / "research"
ADR_DIR = REPO / "docs" / "adr"
ADOPTION_DIR = REPO / "docs" / "research" / "adoptions"

# The reuse classes the plan defines. A mechanism that has not been classified
# has not been decided about.
REUSE_CLASSES = (
    "DEPENDENCY",        # used as published, unmodified
    "ADAPTER",           # used through an adapter we own
    "DIRECT_ADAPT",      # source copied — licence, pin and characterisation first
    "ADAPTIVE_REIMPLEMENT",  # rebuilt because the original does not fit
    "STANDARD",          # a specification, not an implementation
    "BENCHMARK",         # used to measure, not to run
    "PATTERN",           # an idea taken, no code
    "OPTIONAL_BACKEND",  # one of several interchangeable choices
    "DEFER",             # deliberately not decided yet
    "REJECTED",          # considered and refused
)

# A DIRECT_ADAPT cannot be recorded without these, because copying source under
# an unread licence is the one reuse decision that cannot be undone by deleting
# the code later.
DIRECT_ADAPT_REQUIRED = ("licence", "pinned_revision", "copied_files",
                         "characterisation_tests", "attribution")

DISPOSITIONS = ("STILL_VALID", "SUPERSEDED", "NEEDS_RECHECK", "UNVERIFIED")


class ResearchError(RuntimeError):
    """A record was refused because it would not have said anything."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


@dataclass
class Finding:
    question: str
    answer: str
    source: str
    disposition: str = "UNVERIFIED"

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ResearchLog:
    topic: str
    upstream: str | None = None
    revision: str | None = None
    findings: list[Finding] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    changed_the_plan: bool = False
    adr: str | None = None
    recorded_at: str = ""

    def as_dict(self) -> dict:
        d = asdict(self)
        d["schema"] = "dume.research_log/1"
        return d


def live_revision(url: str, ref: str = "HEAD", timeout: int = 30) -> str | None:
    """What the upstream serves right now. None when it could not be reached —
    never a guess, because an unreachable upstream reported as agreement is how
    a drift check comes to mean nothing."""
    try:
        result = subprocess.run(["git", "ls-remote", url, ref],
                                capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.split()[0]


def record_log(topic: str, findings: list[dict], *, upstream: str | None = None,
               revision: str | None = None, unresolved: list[str] | None = None,
               adr: str | None = None, path: Path | None = None) -> Path:
    """Write one research log entry."""
    if not findings and not (unresolved or []):
        raise ResearchError(
            "a research log with neither a finding nor an unresolved question "
            "records that nobody looked")
    parsed = []
    for entry in findings:
        finding = Finding(**entry) if not isinstance(entry, Finding) else entry
        if finding.disposition not in DISPOSITIONS:
            raise ResearchError(
                f"{finding.disposition!r} is not a disposition; expected one of "
                + ", ".join(DISPOSITIONS))
        if not finding.source:
            raise ResearchError(
                f"the finding {finding.question!r} names no source. A claim "
                "without a source is where it came from being forgotten.")
        parsed.append(finding)

    log = ResearchLog(topic=topic, upstream=upstream,
                      revision=revision or (live_revision(upstream) if upstream else None),
                      findings=parsed, unresolved=list(unresolved or []),
                      changed_the_plan=bool(adr), adr=adr, recorded_at=_now())
    from .state import json_dump
    out = path or (RESEARCH_DIR / f"{_slug(topic)}.json")
    json_dump(log.as_dict(), out)
    return out


def record_adoption(name: str, *, reuse_class: str, upstream: str,
                    pinned_revision: str | None = None, licence: str | None = None,
                    capability: str = "", authority_boundary: str = "",
                    copied_files: list[str] | None = None,
                    characterisation_tests: list[str] | None = None,
                    attribution: str | None = None,
                    path: Path | None = None) -> Path:
    """Classify an external mechanism, refusing an unsupportable classification."""
    if reuse_class not in REUSE_CLASSES:
        raise ResearchError(
            f"{reuse_class!r} is not a reuse class; expected one of "
            + ", ".join(REUSE_CLASSES))
    record = {
        "schema": "dume.adoption/1", "name": name, "reuse_class": reuse_class,
        "upstream": upstream, "pinned_revision": pinned_revision,
        "licence": licence, "capability": capability,
        "authority_boundary": authority_boundary or
            "supplies a mechanism; carries no AETHRIONIS authority",
        "copied_files": copied_files or [],
        "characterisation_tests": characterisation_tests or [],
        "attribution": attribution, "recorded_at": _now(),
    }
    if reuse_class == "DIRECT_ADAPT":
        missing = [f for f in DIRECT_ADAPT_REQUIRED if not record.get(f)]
        if missing:
            raise ResearchError(
                "a DIRECT_ADAPT needs " + ", ".join(missing) + " before any code "
                "moves. Copying source under an unread licence is the one reuse "
                "decision that deleting the code later does not undo.")
    if reuse_class in {"DEPENDENCY", "ADAPTER", "OPTIONAL_BACKEND"} and not pinned_revision:
        raise ResearchError(
            f"a {reuse_class} without a pinned revision is a dependency on "
            "whatever upstream happens to be serving that day")
    from .state import json_dump
    out = path or (ADOPTION_DIR / f"{_slug(name)}.json")
    json_dump(record, out)
    return out


def next_adr_number(directory: Path | None = None) -> int:
    directory = directory or ADR_DIR
    numbers = [int(m.group(1)) for p in directory.glob("ADR-*.md")
               if (m := re.match(r"ADR-(\d+)", p.name))]
    return (max(numbers) + 1) if numbers else 1


def draft_adr(title: str, *, context: str, decision: str, consequences: str,
              scope: str = "", supersedes: str = "", evidence: str = "",
              directory: Path | None = None) -> Path:
    """Create a decision record. It cannot be written without its evidence.

    A decision with no finding behind it is a preference wearing a document's
    clothes, and preferences do not need to be superseded — which is exactly why
    they must not be recorded as if they do.
    """
    if not evidence.strip():
        raise ResearchError(
            "an ADR with no evidence is a preference. Name what was measured, "
            "read or run.")
    directory = directory or ADR_DIR
    directory.mkdir(parents=True, exist_ok=True)
    number = next_adr_number(directory)
    path = directory / f"ADR-{number:04d}-{_slug(title)}.md"
    body = [f"# DUME-ADR-{number:04d} — {title}", "",
            "- **Status:** ACCEPTED", f"- **Date:** {_now()[:10]}"]
    if scope:
        body.append(f"- **Scope:** {scope}")
    if supersedes:
        body.append(f"- **Supersedes:** {supersedes}")
    body += ["", "## Context", "", context.strip(), "",
             "## Decision", "", decision.strip(), "",
             "## Consequences", "", consequences.strip(), "",
             "## Evidence", "", evidence.strip(), ""]
    path.write_text("\n".join(body))
    return path


def survey(directory: Path | None = None) -> dict:
    """What has been researched, decided and adopted so far."""
    logs = sorted((directory or RESEARCH_DIR).glob("*.json")) if (directory or RESEARCH_DIR).is_dir() else []
    adoptions = sorted(ADOPTION_DIR.glob("*.json")) if ADOPTION_DIR.is_dir() else []
    adrs = sorted(ADR_DIR.glob("*.md")) if ADR_DIR.is_dir() else []
    by_class: dict[str, list[str]] = {}
    unpinned: list[str] = []
    for path in adoptions:
        try:
            record = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        by_class.setdefault(record["reuse_class"], []).append(record["name"])
        if not record.get("pinned_revision"):
            unpinned.append(record["name"])
    unresolved = []
    for path in logs:
        try:
            record = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        unresolved += [f"{record['topic']}: {q}" for q in record.get("unresolved", [])]
    return {
        "schema": "dume.research_survey/1",
        "logs": [p.stem for p in logs],
        "adoptions_by_class": by_class,
        "unpinned_adoptions": unpinned,
        "adrs": [p.stem for p in adrs],
        "open_questions": unresolved,
    }
