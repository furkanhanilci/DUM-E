"""WP-002 — workspace boundary and read-only specification mount.

Three workspaces, one rule each: the specification is read-only to every build
agent, the target is written only through an assigned worktree, and DUM-E's own
source is never modified as a side effect of a target work package.

The boundary is a *mechanism*, not a instruction to the agent. ``guard`` resolves
symlinks before deciding, so a symlink planted inside an allowed workspace cannot
be used to reach a protected one, and a ``../`` traversal cannot escape.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config


class BoundaryViolation(PermissionError):
    """A write was refused because it left its allowed workspace."""


class WorkspaceUnbound(RuntimeError):
    """The workspace exists as a configuration slot but no path is bound to it."""


@dataclass(frozen=True)
class Decision:
    allowed: bool
    workspace: str | None
    reason: str

    def __bool__(self) -> bool:
        return self.allowed


def _resolve(path: Path | str) -> Path:
    """Resolve to a real absolute path.

    ``strict=False`` so a not-yet-created file still resolves; the *parent*
    chain is what decides containment, and every existing component of that
    chain has its symlinks followed.
    """
    return Path(path).expanduser().resolve(strict=False)


def _contains(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


class Boundary:
    """The write boundary derived from configuration."""

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or config.load()
        self._roots: list[tuple[str, Path, str]] = []
        for name, ws in self.cfg["workspaces"].items():
            if not ws.get("bound") or not ws.get("path"):
                continue
            self._roots.append((name, _resolve(ws["path"]), ws["mode"]))
        # Longest path first: a workspace nested inside another must win, so
        # that evidence/ inside DUM-E is judged APPEND_ONLY, not READ_WRITE.
        self._roots.sort(key=lambda r: len(str(r[1])), reverse=True)

    # ---- queries --------------------------------------------------------

    def locate(self, path: Path | str) -> tuple[str, Path, str] | None:
        target = _resolve(path)
        for name, root, mode in self._roots:
            if target == root or _contains(root, target):
                return name, root, mode
        return None

    def check_write(self, path: Path | str) -> Decision:
        """May this path be written?"""
        target = _resolve(path)
        found = self.locate(target)
        if found is None:
            return Decision(False, None, f"{target} is outside every bound workspace")
        name, _root, mode = found
        if mode == "READ_ONLY":
            return Decision(False, name, f"workspace {name} is READ_ONLY")
        if mode == "APPEND_ONLY" and target.exists() and target.is_file():
            return Decision(
                False, name,
                f"workspace {name} is APPEND_ONLY and {target.name} already exists; "
                "prior evidence is never overwritten")
        return Decision(True, name, f"workspace {name} is {mode}")

    def check_read(self, path: Path | str) -> Decision:
        target = _resolve(path)
        found = self.locate(target)
        if found is None:
            return Decision(False, None, f"{target} is outside every bound workspace")
        return Decision(True, found[0], f"workspace {found[0]} is readable")

    # ---- enforcement ----------------------------------------------------

    def guard_write(self, path: Path | str) -> Path:
        """Return the resolved path, or raise. Call this before every write."""
        decision = self.check_write(path)
        if not decision:
            raise BoundaryViolation(f"write refused: {decision.reason}")
        return _resolve(path)

    def unbound(self) -> list[str]:
        return [n for n, w in self.cfg["workspaces"].items() if not w.get("bound")]


def mount_is_read_only(path: Path | str) -> bool | None:
    """Is this path on a filesystem the kernel itself mounts read-only?

    Returns ``None`` when the answer cannot be determined. Prose in a prompt is
    not a read-only mount, and this is how DUM-E tells the two apart: a truly
    protected specification is one the operating system refuses to write, not
    one the agent has been asked not to write.
    """
    target = _resolve(path)
    if not target.exists():
        return None
    try:
        out = subprocess.run(
            ["findmnt", "-no", "OPTIONS", "--target", str(target)],
            capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    options = out.stdout.strip().split(",")
    return "ro" in options


def probe_write(path: Path | str) -> tuple[str, str]:
    """Actually attempt a write and report what the OS did.

    This is the falsifiable probe. A workspace that *claims* to be read-only and
    accepts a write has failed, and only a real write attempt can show that.

    Returns one of ``WROTE``, ``REFUSED`` or ``MISSING``. The third outcome is
    kept separate on purpose: a directory that does not exist refuses writes for
    a reason that has nothing to do with the boundary, and reporting that as a
    working control would be the exact self-congratulation this harness exists
    to prevent.
    """
    root = _resolve(path)
    if not root.is_dir():
        return "MISSING", f"{root} does not exist, so nothing was proven"
    target = root / ".dume-write-probe"
    try:
        target.write_text("probe\n")
    except OSError as exc:
        return "REFUSED", f"{type(exc).__name__}: {exc}"
    try:
        os.unlink(target)
    except OSError:
        pass
    return "WROTE", "write succeeded"
