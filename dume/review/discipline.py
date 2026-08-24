"""Proving the engineering discipline was applied, not merely available.

Superpowers ships fourteen skills and a SessionStart hook that re-injects the
bootstrap after a compaction. What it does not ship — and says so — is any
machine gate. Every stage boundary in it is prose the model may skip.

So the harness supplies the proof. This module reads the signals a session
leaves behind and reports which stages actually happened. The distinction it
maintains throughout:

* An **invocation signal** (a `Skill` tool call, a hook response) proves the
  skill was entered. It does not prove the model obeyed its content.
* An **artefact signal** (a design document, a plan, a ledger line, a commit
  cadence) proves something was produced.
* Only an **independent signal** — a test that failed before it passed, an exit
  code from a fresh checkout — proves the work is real.

A report that conflated them would let "I invoked test-driven-development" stand
in for "the test failed first", which is exactly the substitution the whole
pipeline exists to prevent.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

PLUGIN_RECORD = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
SETTINGS = Path.home() / ".claude" / "settings.json"
PLUGIN_KEY = "superpowers@claude-plugins-official"

# The stages the commissioning protocol requires, and the skill that covers
# each. Where Superpowers has no skill, the harness owns the stage — saying so
# explicitly is how a gap stays visible instead of being assumed covered.
STAGE_SKILLS: tuple[tuple[str, str | None, str], ...] = (
    ("DESIGN", "superpowers:brainstorming", "a committed design document"),
    ("PLAN", "superpowers:writing-plans", "a plan document"),
    ("WORKTREE", "superpowers:using-git-worktrees", "an isolated worktree"),
    ("RED_GREEN_REFACTOR", "superpowers:test-driven-development",
     "a test that failed before it passed"),
    ("LOCAL_VERIFY", "superpowers:verification-before-completion",
     "a local suite exit code"),
    ("CODE_REVIEW", "superpowers:requesting-code-review", "a review package"),
    ("FRESH_VERIFY", "superpowers:subagent-driven-development",
     "a fresh context re-running the suite"),
    ("MACHINE_GATE", None, "DUM-E's deterministic gate — Superpowers ships none"),
)

SIGNAL_KINDS = ("invocation", "artefact", "independent")


@dataclass
class Signal:
    stage: str
    kind: str
    present: bool
    detail: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class DisciplineReport:
    session_transcript: str | None
    installed_revision: str | None
    expected_revision: str | None
    enabled: bool
    bootstrap_observed: bool
    skills_invoked: list[str] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["signals"] = [s.as_dict() for s in self.signals]
        d["schema"] = "dume.discipline_report/1"
        d["verdict"] = self.verdict()
        return d

    def verdict(self) -> str:
        if self.expected_revision and self.installed_revision != self.expected_revision:
            return "REVISION_MISMATCH"
        if not self.enabled:
            return "NOT_ENABLED"
        independent = [s for s in self.signals if s.kind == "independent"]
        if independent and all(s.present for s in independent):
            return "DISCIPLINE_EVIDENCED"
        if any(s.present for s in self.signals):
            return "INVOKED_BUT_UNPROVEN"
        return "NO_SIGNAL"


def installation(expected_revision: str | None = None) -> dict:
    """What is installed, at which revision, and whether it is switched on.

    A revision is proof of *what* is installed. It is not proof that anything
    used it.
    """
    record = {"installed": False, "revision": None, "version": None,
              "install_path": None, "enabled": False,
              "expected_revision": expected_revision}
    if PLUGIN_RECORD.is_file():
        try:
            data = json.loads(PLUGIN_RECORD.read_text())
        except json.JSONDecodeError:
            data = {}
        entries = _find_key(data, PLUGIN_KEY)
        if entries:
            entry = entries[0] if isinstance(entries, list) else entries
            record.update(installed=True, revision=entry.get("gitCommitSha"),
                          version=entry.get("version"),
                          install_path=entry.get("installPath"))
    if SETTINGS.is_file():
        try:
            settings = json.loads(SETTINGS.read_text())
        except json.JSONDecodeError:
            settings = {}
        record["enabled"] = bool(
            (settings.get("enabledPlugins") or {}).get(PLUGIN_KEY))
    record["revision_matches"] = (
        expected_revision is None or record["revision"] == expected_revision)
    return record


def _find_key(obj, key: str):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = _find_key(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_key(item, key)
            if found is not None:
                return found
    return None


def read_transcript(path: Path | str) -> dict:
    """Extract the invocation signals from a session transcript.

    The transcript is JSONL and may be long; only the fields that carry a
    discipline signal are kept, and a malformed line is skipped rather than
    allowed to abort the read.
    """
    path = Path(path)
    skills, hooks = [], []
    if not path.is_file():
        return {"skills": [], "hooks": [], "lines": 0, "found": False}
    lines = 0
    with path.open(errors="replace") as fh:
        for line in fh:
            lines += 1
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            blob = json.dumps(event)
            for match in re.finditer(r'"skill"\s*:\s*"(superpowers:[a-z\-]+)"', blob):
                skills.append(match.group(1))
            if '"hook_response"' in blob and "using-superpowers" in blob:
                hooks.append("SessionStart bootstrap injected")
    return {"skills": skills, "hooks": hooks, "lines": lines, "found": True}


def artefact_signals(repo: Path | str) -> list[Signal]:
    """What a disciplined run leaves on disk, checked for real content."""
    repo = Path(repo)
    signals = []

    designs = sorted((repo / "docs" / "superpowers" / "specs").glob("*-design.md")) \
        if (repo / "docs" / "superpowers" / "specs").is_dir() else []
    substantive = [d for d in designs if d.stat().st_size > 200]
    signals.append(Signal(
        "DESIGN", "artefact", bool(substantive),
        f"{len(designs)} design document(s), {len(substantive)} with real content"
        if designs else "no design document under docs/superpowers/specs/"))

    plans = sorted((repo / "docs" / "superpowers" / "plans").glob("*.md")) \
        if (repo / "docs" / "superpowers" / "plans").is_dir() else []
    signals.append(Signal(
        "PLAN", "artefact", bool(plans),
        f"{len(plans)} plan document(s)" if plans
        else "no plan under docs/superpowers/plans/"))

    # The SDD ledger, whose lines are deliberately machine-parseable. It is
    # deleted on a clean finish, so its absence is not proof of absence — and
    # saying so is the difference between a report and a guess.
    ledger_lines, parked = [], []
    sdd = repo / ".superpowers" / "sdd"
    if sdd.is_dir():
        for ledger in sdd.rglob("progress.md"):
            for line in ledger.read_text(errors="replace").splitlines():
                if re.match(r"^Task \d+: ", line):
                    ledger_lines.append(line.strip())
                    if "parked" in line:
                        parked.append(line.strip())
    signals.append(Signal(
        "FRESH_VERIFY", "artefact", bool(ledger_lines),
        f"{len(ledger_lines)} ledger entries, {len(parked)} parked"
        if ledger_lines else
        "no SDD ledger — note it is removed on a clean finish, so absence is "
        "not evidence that the stage was skipped"))
    return signals


def red_green_signal(repo: Path | str, base: str, head: str) -> Signal:
    """Independent proof that a test failed before it passed.

    A commit message claiming RED is not RED. This looks for a commit range
    whose test files changed before the implementation did — the shape a real
    test-first cycle leaves — and reports honestly when the history is too
    coarse to tell.
    """
    repo = Path(repo)
    result = subprocess.run(
        ["git", "-C", str(repo), "log", "--format=%H|%s", "--name-only",
         f"{base}..{head}"], capture_output=True, text=True)
    if result.returncode != 0:
        return Signal("RED_GREEN_REFACTOR", "independent", False,
                      f"git log failed: {result.stderr.strip()}")
    commits, current = [], None
    for line in result.stdout.splitlines():
        if "|" in line and len(line.split("|")[0]) == 40:
            sha, subject = line.split("|", 1)
            current = {"sha": sha, "subject": subject, "files": []}
            commits.append(current)
        elif line.strip() and current is not None:
            current["files"].append(line.strip())
    if not commits:
        return Signal("RED_GREEN_REFACTOR", "independent", False,
                      f"no commits between {base[:8]} and {head[:8]}")
    test_first = any(
        all(re.search(r"(^|/)(test_|tests?/)", f) for f in c["files"]) and c["files"]
        for c in commits)
    return Signal(
        "RED_GREEN_REFACTOR", "independent", test_first,
        f"{len(commits)} commit(s); "
        + ("a test-only commit precedes implementation"
           if test_first else
           "no test-only commit — the history is too coarse to show a red "
           "phase, which is not the same as proving there was none"))


def assess(*, transcript: Path | str | None = None, repo: Path | str | None = None,
           expected_revision: str | None = None,
           base: str | None = None, head: str | None = None) -> DisciplineReport:
    install = installation(expected_revision)
    signals: list[Signal] = []
    skills: list[str] = []
    bootstrap = False

    if transcript:
        read = read_transcript(transcript)
        skills = read["skills"]
        bootstrap = bool(read["hooks"])
        for stage, skill, _artefact in STAGE_SKILLS:
            if skill is None:
                continue
            signals.append(Signal(
                stage, "invocation", skill in skills,
                f"{skills.count(skill)} invocation(s) of {skill}"))

    if repo:
        signals.extend(artefact_signals(repo))
        if base and head:
            signals.append(red_green_signal(repo, base, head))

    gaps = [f"{stage}: {detail}" for stage, skill, detail in STAGE_SKILLS
            if skill is None]
    if not transcript:
        gaps.append("no session transcript was supplied, so no invocation "
                    "signal could be read — installation alone proves nothing "
                    "about behaviour")
    if not any(s.kind == "independent" for s in signals):
        gaps.append("no independent signal was available; invocation and "
                    "artefacts alone cannot show the work is correct")

    return DisciplineReport(
        session_transcript=str(transcript) if transcript else None,
        installed_revision=install["revision"],
        expected_revision=expected_revision,
        enabled=install["enabled"], bootstrap_observed=bootstrap,
        skills_invoked=sorted(set(skills)), signals=signals, gaps=gaps)
