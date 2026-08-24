"""Git worktree isolation and protected-path enforcement.

Every implementation task gets its own worktree on its own branch. Two agents
sharing a checkout is two agents editing each other's files, and no amount of
coordination in a chat channel fixes that — the isolation has to be structural.

Protected paths are enforced *here*, at the point a diff is inspected, rather
than requested of the agent. A package that reached the gate with a frozen
acceptance file modified must be caught by something that reads the diff, not
by something that read the instructions.
"""
from __future__ import annotations

import fnmatch
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path


class WorktreeError(RuntimeError):
    """A worktree operation failed, or would have violated isolation."""


class ProtectedPathViolation(RuntimeError):
    """A candidate touched a path it was forbidden to touch."""


@dataclass
class Worktree:
    task_id: str
    wp_id: str
    path: str
    branch: str
    base_revision: str
    repository: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class DiffReport:
    base: str
    head: str
    files: list[str] = field(default_factory=list)
    violations: list[dict] = field(default_factory=list)
    insertions: int = 0
    deletions: int = 0

    def clean(self) -> bool:
        return not self.violations

    def as_dict(self) -> dict:
        return asdict(self)


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args],
                            capture_output=True, text=True)
    if check and result.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}")
    return result.stdout


class WorktreeManager:
    """Creates, inspects and retires per-task worktrees on a target repository."""

    def __init__(self, repository: Path | str, worktree_root: Path | str,
                 protected_paths: list[str] | None = None):
        self.repository = Path(repository).resolve()
        self.worktree_root = Path(worktree_root).resolve()
        self.protected_paths = list(protected_paths or [])
        if not (self.repository / ".git").exists():
            raise WorktreeError(f"{self.repository} is not a git repository")
        self.worktree_root.mkdir(parents=True, exist_ok=True)

    # ---- lifecycle ------------------------------------------------------

    def head(self) -> str:
        return _git(self.repository, "rev-parse", "HEAD").strip()

    def create(self, task_id: str, wp_id: str, base_revision: str | None = None
               ) -> Worktree:
        """One task, one worktree, one branch off an explicit base."""
        base = base_revision or self.head()
        # Resolve so the recorded base is a revision, not a moving name.
        base = _git(self.repository, "rev-parse", base).strip()
        branch = f"dume/{wp_id}/{task_id}"
        path = self.worktree_root / f"{wp_id}__{task_id}"
        if path.exists():
            raise WorktreeError(
                f"worktree path already exists: {path} — retire it before "
                "reusing the task id, so two runs cannot share a tree")
        _git(self.repository, "worktree", "add", "-b", branch, str(path), base)
        return Worktree(task_id=task_id, wp_id=wp_id, path=str(path),
                        branch=branch, base_revision=base,
                        repository=str(self.repository))

    def list(self) -> list[dict]:
        out = _git(self.repository, "worktree", "list", "--porcelain")
        trees, current = [], {}
        for line in out.splitlines():
            if not line.strip():
                if current:
                    trees.append(current)
                    current = {}
                continue
            key, _, value = line.partition(" ")
            current[key] = value
        if current:
            trees.append(current)
        return trees

    def retire(self, worktree: Worktree, force: bool = False) -> None:
        """Remove the worktree. The branch and its commits survive.

        Evidence outlives the workspace that produced it — a retired worktree
        must not take the candidate with it.
        """
        _git(self.repository, "worktree", "remove",
             *(["--force"] if force else []), worktree.path)

    # ---- candidate inspection -------------------------------------------

    def candidate_revision(self, worktree: Worktree) -> str:
        return _git(Path(worktree.path), "rev-parse", "HEAD").strip()

    def is_dirty(self, worktree: Worktree) -> bool:
        """Uncommitted work is not a candidate. A candidate is a revision."""
        return bool(_git(Path(worktree.path), "status", "--porcelain").strip())

    def diff(self, worktree: Worktree, head: str | None = None) -> DiffReport:
        """What this candidate changed, and whether it was allowed to."""
        head = head or self.candidate_revision(worktree)
        raw = _git(Path(worktree.path), "diff", "--numstat",
                   f"{worktree.base_revision}..{head}")
        report = DiffReport(base=worktree.base_revision, head=head)
        for line in raw.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            added, removed, path = parts
            report.files.append(path)
            report.insertions += int(added) if added.isdigit() else 0
            report.deletions += int(removed) if removed.isdigit() else 0
        report.violations = self.check_protected(report.files)
        return report

    def check_protected(self, files: list[str]) -> list[dict]:
        """Which changed files were forbidden, and by which rule."""
        violations = []
        for path in files:
            for pattern in self.protected_paths:
                if fnmatch.fnmatch(path, pattern) or path.startswith(
                        pattern.rstrip("*").rstrip("/") + "/"):
                    violations.append({"path": path, "pattern": pattern})
                    break
        return violations

    def assert_protected_paths_untouched(self, worktree: Worktree,
                                         head: str | None = None) -> DiffReport:
        report = self.diff(worktree, head)
        if report.violations:
            listing = ", ".join(f"{v['path']} (matched {v['pattern']})"
                                for v in report.violations)
            raise ProtectedPathViolation(
                f"{worktree.wp_id}: candidate {report.head[:12]} modified "
                f"protected path(s): {listing}")
        return report

    def frozen_files_unchanged(self, worktree: Worktree, frozen: dict[str, str],
                               head: str | None = None) -> list[dict]:
        """Compare named files against the digests they had when frozen.

        Stronger than a path rule: a file can be renamed around a glob, but a
        digest that no longer matches is a digest that no longer matches.
        """
        from ..state import sha256_file
        drift = []
        for rel, expected in frozen.items():
            path = Path(worktree.path) / rel
            if not path.is_file():
                drift.append({"path": rel, "problem": "MISSING",
                              "expected_sha256": expected})
                continue
            actual = sha256_file(path)
            if actual != expected:
                drift.append({"path": rel, "problem": "MODIFIED",
                              "expected_sha256": expected, "actual_sha256": actual})
        return drift
