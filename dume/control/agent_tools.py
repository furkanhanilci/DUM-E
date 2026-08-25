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
        "name": "append_file",
        "description": ("Add text to the end of a file inside the task "
                        "worktree, creating it if it does not exist. Use this "
                        "to write a long file in several calls: a single tool "
                        "call carrying a whole long file gets cut off."),
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
        "name": "probe_host",
        "description": ("Look at the machine. Runs one read-only inspection "
                        "command and returns its output. Use this whenever you "
                        "need a real number about this host — never write down "
                        "hardware you have not looked at."),
        "parameters": {"type": "object", "properties": {
            "what": {"type": "string",
                     "enum": ["gpu", "cpu", "memory", "disk", "os", "python"],
                     "description": "Which aspect of the host to inspect."}},
            "required": ["what"]}}},
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
        # Whether this actually changed anything. A model that rewrites a file
        # with the content it already has and runs the tests again gets the
        # same failure, and one run did that thirteen times: the write looked
        # like progress in every log while nothing moved.
        unchanged = target.is_file() and target.read_text() == content
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        self.log.record("write_file", {"path": path, "content": content},
                        "OK", ("wrote the same content again"
                               if unchanged else f"wrote {len(content)} chars"))
        return {"ok": True, "path": path, "bytes": len(content.encode()),
                "changed": not unchanged,
                **({"note": "This is byte-for-byte what the file already "
                            "contained. Running the tests will give you the "
                            "same result. Change something."}
                   if unchanged else {})}

    def append_file(self, path: str, content: str) -> dict:
        """Add to the end of a file.

        A file long enough to be worth splitting is exactly the file that made
        a single write_file call run out of tokens mid-string. The size limit
        is checked against the result, not the addition, so appending is not a
        way around it.
        """
        try:
            target = self._resolve(path)
        except ToolDenied as exc:
            self.log.record("append_file", {"path": path}, "DENIED", str(exc))
            return {"ok": False, "error": str(exc)}
        existing = target.read_text() if target.is_file() else ""
        combined = existing + content
        if len(combined.encode()) > self.max_file_bytes:
            message = (f"refusing to grow {path} to {len(combined)} chars; "
                       f"limit is {self.max_file_bytes}")
            self.log.record("append_file", {"path": path}, "DENIED", message)
            return {"ok": False, "error": message}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(combined)
        self.log.record("append_file", {"path": path, "content": content},
                        "OK", f"appended {len(content)} chars to {path}")
        return {"ok": True, "path": path, "bytes": len(combined.encode())}

    # What may be run to look at the machine, by name. An allow-list rather
    # than a shell: reading the host is a READ, and handing an implementer an
    # arbitrary command to get it would trade a bounded capability for an
    # unbounded one. Every entry is read-only and installs nothing.
    PROBES = {
        "gpu": ["nvidia-smi"],
        "cpu": ["lscpu"],
        "memory": ["free", "-h"],
        "disk": ["df", "-h"],
        "os": ["uname", "-a"],
        "python": ["python3", "--version"],
    }

    def probe_host(self, what: str) -> dict:
        """Run one read-only inspection command and return what it said.

        Without this the implementer was asked to record the host's hardware
        and given no way to look at it, so it wrote down an Intel i7 with 32 GB
        of RAM on a machine with two A5000s. Fabrication was the only way to
        satisfy the instruction. A measurement nobody can take is not a
        requirement, it is an invitation.
        """
        command = self.PROBES.get(what)
        if command is None:
            message = (f"{what!r} is not something that can be probed. "
                       f"Choose one of: {', '.join(sorted(self.PROBES))}")
            self.log.record("probe_host", {"what": what}, "DENIED", message)
            return {"ok": False, "error": message}
        try:
            run = subprocess.run(command, capture_output=True, text=True,
                                 timeout=30)
        except FileNotFoundError:
            message = f"{command[0]} is not installed on this host"
            self.log.record("probe_host", {"what": what}, "UNAVAILABLE", message)
            return {"ok": False, "error": message}
        except subprocess.TimeoutExpired:
            message = f"{command[0]} did not answer within 30 seconds"
            self.log.record("probe_host", {"what": what}, "TIMEOUT", message)
            return {"ok": False, "error": message}
        output = (run.stdout + run.stderr)[:4000]
        self.log.record("probe_host", {"what": what, "command": command},
                        "OK", f"{command[0]}: {len(output)} chars")
        return {"ok": True, "what": what, "command": " ".join(command),
                "exit_code": run.returncode, "output": output}

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
                   "append_file": self.append_file,
                   "probe_host": self.probe_host,
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
