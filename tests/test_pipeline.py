"""The commissioning pipeline: packet, cohort, runtime binding, gate, pilot."""
import json

import pytest

from dume.cohort.compiler import assurance_level, compile_cohort, detect_signals
from dume.cohort.role_registry import ROLES
from dume.packets.wp_packet_builder import PacketBuilder, PacketError
from dume.runtimes.failures import UnclassifiedFailure, classify, retry_decision
from dume.runtimes.profiles import NoEligibleRuntime, Runtime, RuntimeRegistry


@pytest.fixture(scope="module")
def builder():
    return PacketBuilder(spec_revision="test")


# ---- packet -------------------------------------------------------------

def test_a_packet_carries_the_frozen_sources_whole_not_a_summary(builder):
    packet = builder.build("WP-035")
    names = {s.name for s in packet.sections}
    assert names == {"card", "tests", "acceptance"}
    for section in packet.sections:
        assert len(section.text) > 500, f"{section.name} looks truncated"
        assert section.sha256


def test_a_packet_names_what_may_never_be_done(builder):
    packet = builder.build("WP-001")
    assert "modify_frozen_acceptance" in packet.forbidden
    assert "accept_or_review_own_work" in packet.forbidden
    assert len(packet.non_waivable_rules) == 25


def test_a_packet_carries_its_dependencies_delivered_outputs(builder):
    packet = builder.build("WP-035")
    assert {d["wp_id"] for d in packet.dependencies} == {"WP-028", "WP-004"}
    assert all(d["required_outputs"] for d in packet.dependencies)


def test_the_packet_digest_changes_when_the_content_does(builder):
    a = builder.build("WP-001")
    b = builder.build("WP-002")
    assert a.packet_sha256 != b.packet_sha256


def test_every_package_in_the_plan_can_be_packeted(builder):
    """A packet builder that works for the easy ones is not a packet builder."""
    for n in range(1, 55):
        packet = builder.build(f"WP-{n:03d}")
        assert packet.sections and packet.packet_sha256


def test_a_package_outside_the_plan_is_refused(builder):
    with pytest.raises(PacketError, match="not in the commissioning plan"):
        builder.build("WP-999")


# ---- cohort -------------------------------------------------------------

def test_signals_come_from_package_specific_text_not_shared_boilerplate(builder):
    """Every card in the plan carries the same scaffolding. If signals were read
    from it, all 54 packages would look identical."""
    levels = {assurance_level(detect_signals(builder.build(f"WP-{n:03d}")))
              for n in range(1, 55)}
    assert len(levels) >= 3, "the detector is not distinguishing packages"


def test_a_trust_boundary_package_is_never_the_cheapest_tier(builder):
    cohort = compile_cohort(builder.build("WP-043"))
    assert cohort.signals["touches_security_boundary"]
    assert cohort.assurance_level != "BASELINE"


def test_every_cohort_has_the_three_distinct_review_roles(builder):
    for wp in ("WP-001", "WP-029", "WP-044"):
        ids = compile_cohort(builder.build(wp)).role_ids()
        assert {"spec_reviewer", "code_reviewer", "verifier"} <= set(ids)
        assert "implementer" in ids


def test_high_assurance_adds_a_second_verifier(builder):
    cohort = compile_cohort(builder.build("WP-044"))
    if cohort.assurance_level == "HIGH":
        assert cohort.role_ids().count("verifier") == 2


def test_the_manifest_states_both_kinds_of_independence(builder):
    cohort = compile_cohort(builder.build("WP-001"))
    matrix = " ".join(cohort.independence_matrix)
    assert "identity: implementer ≠" in matrix or "identity:" in matrix
    assert "family:" in matrix


def test_context_projection_withholds_other_agents_history(builder):
    projection = compile_cohort(builder.build("WP-001")).context_projection
    withheld = " ".join(projection["nobody_receives"])
    assert "conversation history" in withheld
    assert "credential" in withheld


# ---- runtime binding ----------------------------------------------------

def _registry(**overrides):
    roles = ["implementer", "spec_reviewer", "code_reviewer", "verifier"]
    runtimes = [
        Runtime("a1", "p", "m", "AVAILABLE", qualified_roles=roles, family="A", cost_tier=1),
        Runtime("b1", "p", "m", "AVAILABLE", qualified_roles=roles, family="B", cost_tier=2),
    ]
    for rt in runtimes:
        for key, value in overrides.get(rt.runtime_id, {}).items():
            setattr(rt, key, value)
    return RuntimeRegistry(runtimes)


def test_two_agents_may_share_a_runtime_because_a_role_is_not_a_runtime():
    """Identity independence is guaranteed by minting one agent per slot, not by
    consuming one provider per role."""
    reg = _registry()
    impl = reg.bind("implementer", agent_id="wp/implementer")
    reviewer = reg.bind("spec_reviewer", already_bound={"implementer": impl},
                        family_independent_of=("implementer",),
                        agent_id="wp/spec_reviewer")
    assert impl.agent_id != reviewer.agent_id
    assert reviewer.family != impl.family


def test_a_reviewer_never_shares_the_implementers_model_family():
    """Same family, same blind spot: its PASS is not independent evidence."""
    reg = _registry()
    impl = reg.bind("implementer", agent_id="i")
    for role in ("spec_reviewer", "code_reviewer", "verifier"):
        bound = reg.bind(role, already_bound={"implementer": impl},
                         family_independent_of=("implementer",), agent_id=role)
        assert bound.family != impl.family


def test_an_unqualified_runtime_is_not_eligible_however_available_it_is():
    reg = RuntimeRegistry([Runtime("x", "p", "m", "AVAILABLE", qualified_roles=[])])
    with pytest.raises(NoEligibleRuntime, match="not qualified"):
        reg.bind("implementer")


def test_unknown_status_is_never_treated_as_available():
    reg = RuntimeRegistry([Runtime("x", "p", "m", "UNKNOWN",
                                   qualified_roles=["implementer"])])
    with pytest.raises(NoEligibleRuntime):
        reg.bind("implementer")


def test_a_reserved_runtime_is_spent_only_on_what_needs_it():
    """The operator says a premium quota is nearly gone; RESERVE keeps it for
    the work that actually requires it."""
    reg = _registry()
    reg.set_mode("a1", "RESERVE")
    reg.set_status("b1", "QUOTA_EXHAUSTED", reason="quota gone")
    with pytest.raises(NoEligibleRuntime, match="RESERVE"):
        reg.bind("implementer", work_class="ROUTINE_IMPLEMENTATION")
    assert reg.bind("implementer", work_class="ARCHITECTURE_CRITICAL").runtime_id == "a1"


def test_refusal_explains_itself_per_runtime():
    """BLOCKED_RUNTIME with no reason is an outage report nobody can act on."""
    reg = _registry()
    reg.set_status("a1", "QUOTA_EXHAUSTED", reason="billing period")
    reg.set_status("b1", "AUTH_FAILED", reason="token expired")
    with pytest.raises(NoEligibleRuntime) as exc:
        reg.bind("implementer")
    message = str(exc.value)
    assert "QUOTA_EXHAUSTED" in message and "AUTH_FAILED" in message
    assert "assurance does not shrink" in message


def test_a_local_runtime_is_preferred_over_a_remote_one_at_equal_cost():
    reg = RuntimeRegistry([
        Runtime("remote", "p", "m", "AVAILABLE", qualified_roles=["implementer"],
                family="R", cost_tier=1),
        Runtime("local", "p", "m", "AVAILABLE", qualified_roles=["implementer"],
                family="L", cost_tier=1, local=True)])
    assert reg.bind("implementer").runtime_id == "local"


# ---- failure taxonomy ---------------------------------------------------

def test_a_quota_failure_does_not_implicate_the_candidate():
    assert classify("RUNTIME_FAILURE").candidate_implicated is False
    assert classify("IMPLEMENTATION_FAILURE").candidate_implicated is True


def test_a_transient_failure_may_re_run_the_same_candidate():
    assert "the same candidate may be re-run" in retry_decision("RUNTIME_FAILURE", 0)["reason"]
    assert "must change first" in retry_decision("IMPLEMENTATION_FAILURE", 0)["reason"]


def test_a_contradiction_escalates_rather_than_retrying():
    decision = retry_decision("ACCEPTANCE_CONTRADICTION", 0)
    assert decision["retry"] is False
    assert decision["escalate_to"] == "human_commander"


def test_an_unclassified_failure_is_not_a_report():
    with pytest.raises(UnclassifiedFailure):
        classify("it broke")
