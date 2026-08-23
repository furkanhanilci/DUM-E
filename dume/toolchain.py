"""WP-004 — pinned toolchain, reproducible environment and provenance lock.

The lock answers one question: *is the environment that produced this evidence
the environment that is running now?* Evidence produced under a different
toolchain is evidence about a different system, so the verify path reports drift
rather than quietly passing.

Requirements are declared, not discovered. A tool that the commissioning host
needs and does not have is a finding at bring-up, not a traceback three work
packages later.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

LOCK_PATH = Path(__file__).resolve().parent.parent / "config" / "toolchain.lock.json"


@dataclass(frozen=True)
class Tool:
    name: str
    version_args: tuple[str, ...]
    required_for: str
    required: bool = True
    # Some tools are needed only once a later wave begins; recording *when* keeps
    # a wave-9 dependency from blocking wave 1.
    needed_from_wave: int = 1
    version_pattern: str = r"(\d+\.\d+(?:\.\d+)?)"


REQUIRED: tuple[Tool, ...] = (
    Tool("git", ("--version",), "worktree isolation and candidate identity"),
    Tool("python3", ("--version",), "the DUM-E harness itself"),
    Tool("findmnt", ("--version",), "read-only mount verification (WP-002)"),
    Tool("curl", ("--version",), "upstream fetch and health probing"),
    Tool("jq", ("--version",), "deterministic JSON handling in runbooks", required=False),
    Tool("uv", ("--version",), "reproducible Python environments", required=False),
    Tool("nvidia-smi", ("--version",), "GPU capacity probing (WP-001)"),
    Tool("docker", ("--version",), "Buzz relay and serving containers",
         required=False, needed_from_wave=5),
    Tool("cargo", ("--version",), "building Buzz from source (WP-011)",
         required=False, needed_from_wave=4),
    Tool("node", ("--version",), "harness tooling", required=False, needed_from_wave=4),
    Tool("sqlite3", ("--version",), "inspecting DUM-E state by hand",
         required=False, needed_from_wave=1),
)


def _probe(tool: Tool) -> dict:
    path = shutil.which(tool.name)
    entry: dict = {
        "name": tool.name,
        "required": tool.required,
        "required_for": tool.required_for,
        "needed_from_wave": tool.needed_from_wave,
        "present": path is not None,
        "path": path,
        "version_raw": None,
        "version": None,
    }
    if path is None:
        return entry
    try:
        p = subprocess.run([path, *tool.version_args], capture_output=True,
                           text=True, timeout=20)
        combined = ((p.stdout or "") + (p.stderr or "")).strip()
        raw = combined.splitlines()
        entry["version_raw"] = raw[0] if raw else None
        # Some tools put the version on a later line (nvidia-smi prints a
        # banner first), so the whole output is searched, not just line one.
        entry["_version_search"] = combined
    except (OSError, subprocess.TimeoutExpired) as exc:
        entry["version_raw"] = f"probe failed: {exc}"
        return entry
    haystack = entry.pop("_version_search", entry["version_raw"] or "")
    m = re.search(tool.version_pattern, haystack)
    entry["version"] = m.group(1) if m else None
    return entry


def environment_digest(tools: list[dict]) -> str:
    """A stable digest of the facts that change how a run behaves."""
    material = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "tools": {t["name"]: t["version"] for t in tools if t["present"]},
    }
    blob = json.dumps(material, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()


def collect(current_wave: int = 1) -> dict:
    tools = [_probe(t) for t in REQUIRED]
    missing_required = [
        t["name"] for t, spec in zip(tools, REQUIRED)
        if spec.required and spec.needed_from_wave <= current_wave and not t["present"]
    ]
    missing_later = [
        {"name": t["name"], "needed_from_wave": t["needed_from_wave"],
         "required_for": t["required_for"]}
        for t, spec in zip(tools, REQUIRED)
        if not t["present"] and spec.needed_from_wave > current_wave
    ]
    optional_missing = [
        {"name": t["name"], "required_for": t["required_for"]}
        for t, spec in zip(tools, REQUIRED)
        if not t["present"] and not spec.required
        and spec.needed_from_wave <= current_wave
    ]
    return {
        "schema": "dume.toolchain_lock/1",
        "current_wave": current_wave,
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "python_executable": os.sys.executable,
        "tools": tools,
        "missing_required": missing_required,
        "missing_for_later_waves": missing_later,
        "optional_missing": optional_missing,
        "environment_digest": environment_digest(tools),
    }


def write_lock(current_wave: int = 1, path: Path | None = None) -> dict:
    lock = collect(current_wave)
    path = Path(path) if path else LOCK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
    return lock


def verify(path: Path | None = None) -> dict:
    """Compare the live environment against the lock and report every drift."""
    path = Path(path) if path else LOCK_PATH
    if not path.is_file():
        return {"status": "NO_LOCK", "detail": f"no toolchain lock at {path}"}
    locked = json.loads(path.read_text())
    live = collect(locked.get("current_wave", 1))
    drift: list[dict] = []
    locked_tools = {t["name"]: t for t in locked["tools"]}
    for tool in live["tools"]:
        was = locked_tools.get(tool["name"])
        if was is None:
            drift.append({"tool": tool["name"], "change": "APPEARED_IN_LIVE"})
            continue
        if was["present"] != tool["present"]:
            drift.append({"tool": tool["name"], "change": "PRESENCE_CHANGED",
                          "locked": was["present"], "live": tool["present"]})
        elif was["version"] != tool["version"]:
            drift.append({"tool": tool["name"], "change": "VERSION_CHANGED",
                          "locked": was["version"], "live": tool["version"]})
    return {
        "status": "DRIFT" if drift else "MATCH",
        "locked_digest": locked["environment_digest"],
        "live_digest": live["environment_digest"],
        "drift": drift,
        "lock_path": str(path),
    }
