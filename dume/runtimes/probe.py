"""Probe what can actually run on this host.

Everything here is measured. A runtime is `RUNTIME_MISSING` because its command
is not on PATH, `AVAILABLE` because an endpoint answered, and `UNKNOWN` when
neither could be established — never `AVAILABLE` because a configuration file
said so.

Probing tells you a runtime *can run*. It does not tell you the runtime is
*good enough for a role*: that is qualification, and it is measured separately.
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone

from .profiles import Runtime, RuntimeRegistry

# How each runtime is detected. A CLI on PATH, or an OpenAI-compatible endpoint
# that answers /v1/models.
CLI_PROBES = {
    "claude-opus-5": "claude",
    "claude-fable-5": "claude",
    "claude-sonnet-5": "claude",
    "codex-terra": "codex",
    "codex-sol": "codex",
}

ENDPOINT_PROBES = {
    "qwen-local": ("http://127.0.0.1:8000/v1/models",
                   "http://127.0.0.1:30000/v1/models",
                   "http://127.0.0.1:8080/v1/models"),
    "hermes": ("http://127.0.0.1:5000/v1/models",),
}


def _cli_present(command: str) -> tuple[bool, str]:
    path = shutil.which(command)
    if not path:
        return False, f"{command} is not on PATH"
    return True, path


def _endpoint_answers(url: str, timeout: float = 2.0) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read(4096).decode("utf-8", "replace")
        try:
            models = [m.get("id") for m in json.loads(body).get("data", [])]
        except (json.JSONDecodeError, AttributeError):
            models = []
        return True, f"answered with {len(models)} model(s): {', '.join(filter(None, models))[:80]}"
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def probe(registry: RuntimeRegistry) -> dict:
    """Update every runtime's status from what the host can actually reach."""
    results = []
    for rt in registry.runtimes.values():
        if rt.runtime_id in ENDPOINT_PROBES:
            reached = None
            for url in ENDPOINT_PROBES[rt.runtime_id]:
                ok, detail = _endpoint_answers(url)
                if ok:
                    reached = (url, detail)
                    break
            if reached:
                rt.status, rt.reason = "AVAILABLE", f"{reached[0]} {reached[1]}"
            else:
                rt.status = "RUNTIME_MISSING"
                rt.reason = ("no OpenAI-compatible endpoint answered on "
                             + ", ".join(ENDPOINT_PROBES[rt.runtime_id]))
        elif rt.runtime_id in CLI_PROBES:
            ok, detail = _cli_present(CLI_PROBES[rt.runtime_id])
            if ok:
                # The CLI exists. Whether this account still has quota is not
                # something a probe can know without spending some, so the
                # honest answer is UNKNOWN rather than AVAILABLE.
                rt.status = "UNKNOWN"
                rt.reason = (f"{detail} is installed, but quota and auth cannot "
                             "be established without spending a request")
            else:
                rt.status, rt.reason = "RUNTIME_MISSING", detail
        else:
            rt.status, rt.reason = "UNKNOWN", "no probe is defined for this runtime"
        results.append({"runtime": rt.runtime_id, "status": rt.status,
                        "reason": rt.reason})
    return {
        "schema": "dume.runtime_probe/1",
        "probed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "results": results,
        "usable_now": sorted(r.runtime_id for r in registry.runtimes.values()
                             if r.usable()),
        "qualified_for_any_role": sorted(
            r.runtime_id for r in registry.runtimes.values() if r.qualified_roles),
    }
