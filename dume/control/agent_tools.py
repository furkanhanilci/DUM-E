"""The tools an implementing agent is given, and the boundary they run inside.

Invariant 18 and WP-043: an agent receives only the paths and capabilities its
task requires. The enforcement is here, in the tool implementation, not in the
prompt — a capability an agent has been *asked* not to use is a capability it
has.

Every tool resolves its path and refuses anything outside the worktree it was
given. A `../` escape and a planted symlink are both handled by resolving
before deciding, which is the same rule the workspace boundary uses, applied at
the narrower scope of one task.
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


class ToolDenied(PermissionError):
    """A tool call was refused. The message says which boundary refused it."""


TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Create or overwrite a file inside the task worktree.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string",
                     "description": "Path relative to the worktree root."},
            "content": {"type": "string"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a file inside the task worktree.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "list_files",
        "description": "List files in the task worktree.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "run_tests",
        "description": ("Run the test suite in the worktree and return its exit "
                        "code and output. This is the only thing that decides "
                        "whether a test passes."),
        "parameters": {"type": "object", "properties": {}}}},
]


@dataclass
class ToolLog:
    """What the agent actually did, for the evidence record."""
    calls: list[dict] = field(default_factory=list)

    def record(self, name: str, arguments: dict, outcome: str, detail: str) -> None:
        redacted = dict(arguments)
        if "content" in redacted:
            redacted["content"] = f"<{len(str(redacted['content']))} chars>"
        self.calls.append({"tool": name, "arguments": redacted,
                           "outcome": outcome, "detail": detail[:400]})

    def test_runs(self) -> list[dict]:
        return [c for c in self.calls if c["tool"] == "run_tests"]


class Toolbox:
    """Tools bound to one worktree. Nothing here can reach outside it."""

    def __init__(self, worktree_path: Path | str, log: ToolLog | None = None,
                 max_file_bytes: int = 256 * 1024):
        self.root = Path(worktree_path).resolve()
        self.log = log or ToolLog()
        self.max_file_bytes = max_file_bytes

    def _resolve(self, relative: str) -> Path:
        candidate = (self.root / relative).expanduser().resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            raise ToolDenied(
                f"{relative!r} resolves to {candidate}, outside the task "
                f"worktree {self.root}") from None
        if candidate == self.root:
            raise ToolDenied("the worktree root is not a file")
        # The agent may not rewrite git's own state to fake a candidate.
        if ".git" in candidate.parts:
            raise ToolDenied(f"{relative!r} is inside .git; the candidate is "
                             "produced by committing, not by editing history")
        return candidate

    # ---- tools ----------------------------------------------------------

    def write_file(self, path: str, content: str) -> dict:
        try:
            target = self._resolve(path)
        except ToolDenied as exc:
            self.log.record("write_file", {"path": path}, "DENIED", str(exc))
            return {"ok": False, "error": str(exc)}
        if len(content.encode()) > self.max_file_bytes:
            message = f"refusing {len(content)} chars; limit is {self.max_file_bytes}"
            self.log.record("write_file", {"path": path}, "DENIED", message)
            return {"ok": False, "error": message}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        self.log.record("write_file", {"path": path, "content": content},
                        "OK", f"wrote {len(content)} chars")
        return {"ok": True, "path": path, "bytes": len(content.encode())}

    def read_file(self, path: str) -> dict:
        try:
            target = self._resolve(path)
        except ToolDenied as exc:
            self.log.record("read_file", {"path": path}, "DENIED", str(exc))
            return {"ok": False, "error": str(exc)}
        if not target.is_file():
            self.log.record("read_file", {"path": path}, "MISSING", "no such file")
            return {"ok": False, "error": f"{path} does not exist"}
        text = target.read_text(errors="replace")[:self.max_file_bytes]
        self.log.record("read_file", {"path": path}, "OK", f"{len(text)} chars")
        return {"ok": True, "path": path, "content": text}

    def list_files(self) -> dict:
        files = sorted(
            str(p.relative_to(self.root)) for p in self.root.rglob("*")
            if p.is_file() and ".git" not in p.parts)
        self.log.record("list_files", {}, "OK", f"{len(files)} file(s)")
        return {"ok": True, "files": files[:200]}

    def run_tests(self) -> dict:
        """Run the suite. The exit code is the evidence; nothing else is."""
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", str(self.root)],
            cwd=str(self.root), capture_output=True, text=True, timeout=600)
        output = (result.stdout + result.stderr)[-4000:]
        self.log.record("run_tests", {}, "EXIT_%d" % result.returncode,
                        output[-400:])
        return {"ok": True, "exit_code": result.returncode,
                "passed": result.returncode == 0, "output": output[-2500:]}

    # ---- dispatch -------------------------------------------------------

    def dispatch(self, name: str, arguments: dict) -> dict:
        handler = {"write_file": self.write_file, "read_file": self.read_file,
                   "list_files": self.list_files, "run_tests": self.run_tests}.get(name)
        if handler is None:
            self.log.record(name, arguments, "UNKNOWN_TOOL", "no such tool")
            return {"ok": False, "error": f"no such tool: {name}"}
        try:
            return handler(**arguments)
        except TypeError as exc:
            self.log.record(name, arguments, "BAD_ARGUMENTS", str(exc))
            return {"ok": False, "error": f"bad arguments: {exc}"}
        except subprocess.TimeoutExpired:
            self.log.record(name, arguments, "TIMEOUT", "the suite did not finish")
            return {"ok": False, "error": "the test suite timed out"}
