"""A runtime that runs out must hand its role to another one.

Quota is not a property of the work: a package does not become wrong because
someone's billing period turned over. The registry already carries
`QUOTA_EXHAUSTED` and `usable()` already excludes it — these tests hold the
behaviour that depends on them, so a fallback cannot be quietly lost by a change
to eligibility or ordering.
"""
from __future__ import annotations

import pytest

from dume.cohort.role_registry import ROLES
from dume.runtimes.profiles import RuntimeRegistry


def _bind(registry, role_id, already=None, work_class=None):
    role = ROLES[role_id]
    return registry.bind(
        role_id, already_bound=already or {}, work_class=work_class,
        family_independent_of=tuple(getattr(role, "family_independent_of", ()) or ()))


def test_an_exhausted_runtime_is_not_bound():
    registry = RuntimeRegistry.load()
    first = _bind(registry, "implementer").runtime_id

    registry.set_status(first, "QUOTA_EXHAUSTED",
                        reason="test: billing period", retry_after="2026-09-01T00:00:00Z")
    second = _bind(registry, "implementer").runtime_id

    assert second != first, (
        f"{first} was bound again after being marked QUOTA_EXHAUSTED; a role "
        "would keep failing against a runtime that has already refused")


def test_the_fallback_still_satisfies_independence():
    """Falling back must not put a reviewer in the implementer's family.

    The cheapest remaining runtime is not automatically an acceptable one. If
    quota pressure could collapse family diversity, assurance would quietly
    depend on nobody's billing period turning over.
    """
    registry = RuntimeRegistry.load()
    bound = {}
    for role_id in ("architect", "implementer", "spec_reviewer", "code_reviewer", "verifier"):
        bound[role_id] = _bind(registry, role_id, bound)

    exhausted = bound["spec_reviewer"].runtime_id
    registry.set_status(exhausted, "QUOTA_EXHAUSTED", reason="test", retry_after=None)

    again = {k: v for k, v in bound.items() if k != "spec_reviewer"}
    replacement = _bind(registry, "spec_reviewer", again)

    assert replacement.runtime_id != exhausted
    assert replacement.family != bound["implementer"].family, (
        "the fallback put the spec reviewer in the implementer's family; a "
        "reviewer that shares the implementer's blind spots is not evidence")


def test_running_out_everywhere_refuses_rather_than_degrades():
    """When nothing eligible is left, the package waits.

    Assurance does not shrink because the cheap option is unavailable. A
    NoEligibleRuntime is a first-class outcome, and it is the one thing that
    must not be replaced by "use whatever is left".
    """
    from dume.runtimes.profiles import NoEligibleRuntime

    registry = RuntimeRegistry.load()
    for rt in registry.runtimes.values():
        if "verifier" in rt.qualified_roles:
            registry.set_status(rt.runtime_id, "QUOTA_EXHAUSTED", reason="test")

    with pytest.raises(NoEligibleRuntime):
        _bind(registry, "verifier")


def test_recovery_restores_the_runtime():
    """A runtime marked exhausted comes back when it is marked available."""
    registry = RuntimeRegistry.load()
    first = _bind(registry, "implementer").runtime_id
    registry.set_status(first, "QUOTA_EXHAUSTED", reason="test")
    assert _bind(registry, "implementer").runtime_id != first

    registry.set_status(first, "AVAILABLE", reason="test: period turned over")
    assert _bind(registry, "implementer").runtime_id == first, (
        "a recovered runtime did not return to the pool")
