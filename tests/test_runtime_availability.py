"""A runtime that has been measured to work must be usable.

Two ways the harness lost that, both found while binding roles for the Buzz
integration and both silent: the router reported one usable runtime and bound
every role to it, which is a cohort with no independence at all and looks
exactly like a cohort that has it.
"""
from __future__ import annotations

import json

from dume.runtimes import probe as probe_module
from dume.runtimes.profiles import RuntimeRegistry


def test_every_local_serving_runtime_has_a_probe():
    """A local runtime with no probe entry is reported UNKNOWN forever.

    `mistral-local` serves the second family — the one that makes review
    independent of implementation. It answered on :8001 throughout, but it was
    absent from the probe table, so `runtime --probe` overwrote a working
    AVAILABLE with "no probe is defined for this runtime" and the router
    stopped offering it. One family was then left to review its own work,
    which ADR-0008 exists to prevent.
    """
    registry = RuntimeRegistry.load()
    local = [r for r in registry.runtimes.values() if r.local]
    unprobed = [r.runtime_id for r in local
                if r.runtime_id not in probe_module.ENDPOINT_PROBES
                and r.runtime_id not in probe_module.CLI_PROBES]
    assert unprobed == [], (
        f"local runtimes with no probe defined: {unprobed} — "
        "each will be reported UNKNOWN and silently dropped from binding")


def test_mistral_is_probed_on_the_port_it_serves():
    """The address is the one the deployment actually uses."""
    assert "mistral-local" in probe_module.ENDPOINT_PROBES
    assert any(":8001/v1/models" in url
               for url in probe_module.ENDPOINT_PROBES["mistral-local"])


def test_recording_a_qualification_makes_the_runtime_available(tmp_path, monkeypatch):
    """Passing the live trials is stronger evidence than a CLI on PATH.

    `qualify --record` wrote `qualified_roles` and left `status` at UNKNOWN,
    so a runtime that had just answered four live tool-calling trials was
    still "not usable now". The router counts usable runtimes, so recording a
    success made no difference to what could be bound.
    """
    from dume.runtimes import qualification as q

    registry = RuntimeRegistry.load()
    target = "claude-sonnet-5"
    runtime = registry.get(target)
    assert runtime is not None, "fixture assumes this runtime is in the registry"

    runtime.status = "UNKNOWN"
    runtime.qualified_roles = []

    q.record_qualification(registry, target, ["verifier"], evidence="trials passed")

    updated = registry.get(target)
    assert updated.qualified_roles == ["verifier"]
    assert updated.status == "AVAILABLE", (
        "a runtime that passed the live trials must be usable; leaving it "
        "UNKNOWN collapses every role onto whichever runtime happened to probe")
    assert "qualif" in (updated.reason or "").lower(), (
        "the reason must say what made it available, so a later reader can "
        "tell a measured runtime from an assumed one")


def test_probing_does_not_demote_a_qualified_runtime():
    """A weaker check must not overwrite a stronger one.

    A CLI probe can only ever answer UNKNOWN for a runtime reached that way —
    quota and auth cannot be established without spending a request. Running
    `runtime --probe` after a qualification therefore threw the measurement
    away and put the guess back, so recording a pass appeared to work and
    silently stopped mattering the next time anything probed.
    """
    registry = RuntimeRegistry.load()
    target = "claude-sonnet-5"
    runtime = registry.get(target)
    assert runtime is not None

    runtime.qualified_roles = ["verifier"]
    runtime.status = "AVAILABLE"
    runtime.reason = "qualification trials passed: tool_calling"

    probe_module.probe(registry)

    after = registry.get(target)
    assert after.status == "AVAILABLE", (
        "a probe found the CLI present and downgraded a runtime whose live "
        "trials had already passed; the weaker signal must not win")
