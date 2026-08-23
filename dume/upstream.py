"""The upstream lock: which revision of every external mechanism DUM-E is built
against, and whether that revision is still what upstream serves.

Silently upgrading a dependency during a commissioned work package is forbidden.
The lock makes an upgrade a visible, dated decision rather than an accident, and
``check`` is what turns "we pinned it" into "we verified the pin".
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

LOCK_PATH = Path(__file__).resolve().parent.parent / "config" / "upstream.lock.json"


def ls_remote(url: str, ref: str = "HEAD", timeout: int = 30) -> str | None:
    """The revision upstream currently serves for a ref, or None if unreachable.

    Unreachable is reported as unknown, never as agreement: a network failure
    must not be able to look like "no drift".
    """
    try:
        p = subprocess.run(["git", "ls-remote", url, ref],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0 or not p.stdout.strip():
        return None
    return p.stdout.split()[0]


def load(path: Path | None = None) -> dict:
    path = Path(path) if path else LOCK_PATH
    if not path.is_file():
        raise FileNotFoundError(f"no upstream lock at {path}")
    return json.loads(path.read_text())


def check(path: Path | None = None) -> dict:
    """Compare every pinned upstream against what it serves now."""
    lock = load(path)
    results = []
    for entry in lock["upstreams"]:
        pinned = entry.get("pinned_revision")
        if entry.get("resolve_at_execution") or not pinned:
            status = "UNPINNED"
            live = ls_remote(entry["source"], entry.get("ref", "HEAD"))
        else:
            live = ls_remote(entry["source"], entry.get("ref", "HEAD"))
            if live is None:
                status = "UNREACHABLE"
            elif live == pinned:
                status = "NO_DRIFT"
            else:
                status = "DRIFT"
        results.append({
            "name": entry["name"], "source": entry["source"], "role": entry["role"],
            "pinned_revision": pinned, "live_revision": live, "status": status,
            "license": entry.get("license"),
            "reuse_class": entry.get("reuse_class"),
        })
    drifted = [r for r in results if r["status"] == "DRIFT"]
    unreachable = [r for r in results if r["status"] == "UNREACHABLE"]
    return {
        "schema": "dume.upstream_check/1",
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "results": results,
        "drift_count": len(drifted),
        "unreachable_count": len(unreachable),
        "verdict": "DRIFT" if drifted else ("INCOMPLETE" if unreachable else "CLEAN"),
    }
