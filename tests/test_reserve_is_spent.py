"""A reserved runtime must actually be spent on the work it was reserved for.

`reserve` answers "it will now only be spent on architecture-critical work,
spec conflicts and high-risk review" — but it only ever removed the runtime
from the routine pool. On the work it admits, the binder still took the
cheapest candidate, so a premium runtime held back for the hardest decisions
was never reached by them. Reserving changed what the cheap work could use and
nothing about what the expensive work got.
"""
from __future__ import annotations

import pytest

from dume.cohort.role_registry import ROLES
from dume.runtimes.profiles import RuntimeRegistry


def _bind_all(registry, work_class=None):
    order = ["architect", "implementer", "spec_reviewer", "code_reviewer", "verifier"]
    bound = {}
    for role_id in order:
        role = ROLES[role_id]
        bound[role_id] = registry.bind(
            role_id, already_bound=bound, work_class=work_class,
            family_independent_of=tuple(
                getattr(role, "family_independent_of", ()) or ()))
    return bound


def test_routine_work_stays_on_the_cheap_local_runtimes():
    """Nothing reserved is spent on unclassified work."""
    registry = RuntimeRegistry.load()
    reserved = {r.runtime_id for r in registry.runtimes.values() if r.mode == "RESERVE"}
    if not reserved:
        pytest.skip("no runtime is reserved in this registry")

    bound = _bind_all(registry)
    used = {b.runtime_id for b in bound.values()}
    assert used.isdisjoint(reserved), (
        f"routine work reached a reserved runtime: {used & reserved}")
    assert bound["implementer"].runtime_id == "qwen-local", (
        "the implementer is the workhorse; it should stay on the cheapest local "
        "runtime whatever else the package needs")


def test_architecture_critical_work_reaches_a_reserved_runtime():
    """The role the class is about gets what was held back for it.

    Reserving is not only a way to keep premium quota away from routine work.
    The command says the runtime *will be spent* on this, and a control that
    never changes the outcome it names is not a control.
    """
    registry = RuntimeRegistry.load()
    reserved = {r.runtime_id for r in registry.runtimes.values()
                if r.mode == "RESERVE" and "architect" in r.qualified_roles}
    if not reserved:
        pytest.skip("no reserved runtime is qualified to architect")

    bound = _bind_all(registry, work_class="ARCHITECTURE_CRITICAL")
    assert bound["architect"].runtime_id in reserved, (
        f"architecture-critical work bound the architect to "
        f"{bound['architect'].runtime_id}; a runtime was reserved for exactly "
        f"this and was not reached: {sorted(reserved)}")


def test_a_reserved_runtime_does_not_displace_the_workhorse():
    """Only the role the class concerns is upgraded.

    Spending premium quota on the implementer during an architecture-critical
    package would burn the budget on the highest-volume role while changing
    nothing about the decision that made the work critical.
    """
    registry = RuntimeRegistry.load()
    if not any(r.mode == "RESERVE" for r in registry.runtimes.values()):
        pytest.skip("no runtime is reserved in this registry")

    bound = _bind_all(registry, work_class="ARCHITECTURE_CRITICAL")
    implementer = registry.get(bound["implementer"].runtime_id)
    assert implementer.local, (
        "the implementer moved off a local runtime for architecture-critical "
        "work; the workhorse role should not spend external quota")


def test_independence_survives_the_upgrade():
    """Reaching for a reserved runtime must not collapse family independence."""
    registry = RuntimeRegistry.load()
    if not any(r.mode == "RESERVE" for r in registry.runtimes.values()):
        pytest.skip("no runtime is reserved in this registry")

    bound = _bind_all(registry, work_class="ARCHITECTURE_CRITICAL")
    implementer_family = bound["implementer"].family
    for reviewer in ("spec_reviewer", "code_reviewer", "verifier"):
        assert bound[reviewer].family != implementer_family, (
            f"{reviewer} shares the implementer's family after the upgrade")
