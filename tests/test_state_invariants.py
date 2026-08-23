"""The lifecycle controls, exercised as behaviour rather than asserted as prose.

Each test names the invariant it defends. If one of these passes while the
control is removed, the test is wrong.
"""
import pytest

from conftest import (CODE_REVIEWER, PRODUCER, SPEC_REVIEWER, VERIFIER, drive)
from dume.state import StateError, Store


def test_ready_requires_accepted_dependencies(store):
    """READY is a claim about dependencies, so an unaccepted one must refuse it."""
    with pytest.raises(StateError, match="hard dependencies not ACCEPTED"):
        store.transition("WP-002", "READY", actor="human")


def test_the_lifecycle_has_no_shortcut_to_acceptance(store):
    """Three review stages exist because they ask three different questions."""
    store.transition("WP-001", "READY", actor="human")
    with pytest.raises(StateError, match="not a permitted transition"):
        store.transition("WP-001", "ACCEPTED", actor=VERIFIER)


def test_execution_cannot_skip_straight_to_tech_complete(store):
    drive(store, stop_at="reviews")
    with pytest.raises(StateError, match="not a permitted transition"):
        store.transition("WP-001", "TECH_COMPLETE", actor=PRODUCER)


def test_tech_complete_requires_all_three_review_stages(store):
    """A package that skipped code review has not been reviewed for quality."""
    drive(store, stop_at="reviews")
    store.transition("WP-001", "SPEC_REVIEW", actor="orchestrator")
    store.record_review("WP-001", "specification_compliance", "cand-1",
                        SPEC_REVIEWER, "PASS")
    store.transition("WP-001", "CODE_REVIEW", actor="orchestrator")
    store.record_review("WP-001", "code_quality", "cand-1", CODE_REVIEWER, "PASS")
    store.transition("WP-001", "VERIFYING", actor="orchestrator")
    with pytest.raises(StateError, match="no PASSing verification"):
        store.transition("WP-001", "TECH_COMPLETE", actor=PRODUCER)


def test_a_review_stage_out_of_order_is_refused(store):
    """Code review recorded while the package is still executing is a review of
    nothing anyone has declared finished."""
    drive(store, stop_at="reviews")
    with pytest.raises(StateError, match="may only be recorded in state CODE_REVIEW"):
        store.record_review("WP-001", "code_quality", "cand-1", CODE_REVIEWER, "PASS")


def test_producer_cannot_review_its_own_work(store):
    drive(store, stop_at="reviews")
    store.transition("WP-001", "SPEC_REVIEW", actor="orchestrator")
    with pytest.raises(StateError, match="may not perform specification_compliance"):
        store.record_review("WP-001", "specification_compliance", "cand-1",
                            PRODUCER, "PASS")


def test_the_verifier_may_not_be_a_reviewer_who_already_took_a_position(store):
    """Fresh verification checks the work, not the verifier's own conclusion."""
    drive(store, stop_at="reviews")
    store.transition("WP-001", "SPEC_REVIEW", actor="orchestrator")
    store.record_review("WP-001", "specification_compliance", "cand-1",
                        SPEC_REVIEWER, "PASS")
    store.transition("WP-001", "CODE_REVIEW", actor="orchestrator")
    store.record_review("WP-001", "code_quality", "cand-1", CODE_REVIEWER, "PASS")
    store.transition("WP-001", "VERIFYING", actor="orchestrator")
    store.record_review("WP-001", "verification", "cand-1", CODE_REVIEWER, "PASS")
    with pytest.raises(StateError, match="must be independent of the review"):
        store.transition("WP-001", "TECH_COMPLETE", actor=PRODUCER)


def test_producer_cannot_accept_own_package(store):
    drive(store)
    with pytest.raises(StateError, match="may not accept its own package"):
        store.transition("WP-001", "ACCEPTED", actor=PRODUCER)


def test_acceptance_requires_verification_evidence(store, tmp_path):
    """A verdict with no evidence behind it is a claim, not a result."""
    other = Store(tmp_path / "other.db")
    other.register("WP-X", "no reviews", "01_FOUNDATION", 1)
    other.transition("WP-X", "READY", actor="human")
    other.transition("WP-X", "PACKAGED", actor="human")
    other.transition("WP-X", "PLANNED", actor="architect")
    other.transition("WP-X", "EXECUTING", actor=PRODUCER, candidate_revision="c1")
    other.transition("WP-X", "SPEC_REVIEW", actor="orchestrator")
    with pytest.raises(StateError, match="no PASSing specification_compliance"):
        other.transition("WP-X", "CODE_REVIEW", actor="orchestrator")
        other.transition("WP-X", "VERIFYING", actor="orchestrator")
        other.transition("WP-X", "TECH_COMPLETE", actor=PRODUCER)
    other.close()


def test_stale_evidence_from_another_candidate_does_not_carry_over(store):
    """A green result from an older candidate is not evidence for a newer one."""
    drive(store, candidate="cand-1")
    with pytest.raises(StateError, match="does not match the candidate under review"):
        store.transition("WP-001", "ACCEPTED", actor=VERIFIER,
                         candidate_revision="cand-2")


def test_a_failing_verdict_stops_the_pipeline_at_that_stage(store):
    """The failure surfaces where it happened, not three stages later when the
    correction has become expensive."""
    drive(store, stop_at="reviews")
    store.transition("WP-001", "SPEC_REVIEW", actor="orchestrator")
    store.record_review("WP-001", "specification_compliance", "cand-1",
                        SPEC_REVIEWER, "FAIL")
    with pytest.raises(StateError, match="cannot enter CODE_REVIEW"):
        store.transition("WP-001", "CODE_REVIEW", actor="orchestrator")


def test_open_critical_finding_blocks_acceptance(store):
    drive(store)
    store.add_finding("WP-001", "CRITICAL", "boundary can be bypassed by symlink")
    with pytest.raises(StateError, match="open Critical/High finding"):
        store.transition("WP-001", "ACCEPTED", actor=VERIFIER)


def test_happy_path_accepts_and_releases_dependent(store):
    drive(store)
    store.transition("WP-001", "ACCEPTED", actor=VERIFIER)
    assert store.get("WP-001")["state"] == "ACCEPTED"
    store.transition("WP-002", "READY", actor="human")
    assert store.get("WP-002")["state"] == "READY"


def test_a_failure_is_classified_and_retried_through_a_plan(store):
    """A retry re-enters at PLANNED: a correction needs a plan, not a second
    attempt at the same one."""
    drive(store, stop_at="reviews")
    store.transition("WP-001", "FAILED", actor=VERIFIER, reason="T04 failed")
    store.transition("WP-001", "RETRY", actor="orchestrator",
                     reason="IMPLEMENTATION_FAILURE")
    store.transition("WP-001", "PLANNED", actor="architect")
    assert store.get("WP-001")["state"] == "PLANNED"


def test_retry_preserves_prior_failed_evidence(store):
    """A retry adds; it never erases what went wrong the first time."""
    drive(store, stop_at="reviews")
    store.transition("WP-001", "SPEC_REVIEW", actor="orchestrator")
    store.record_review("WP-001", "specification_compliance", "cand-1",
                        SPEC_REVIEWER, "FAIL")
    store.transition("WP-001", "FAILED", actor=SPEC_REVIEWER, reason="requirement missed")
    store.transition("WP-001", "RETRY", actor="orchestrator", reason="SPEC_MISINTERPRETATION")
    store.transition("WP-001", "PLANNED", actor="architect")
    store.transition("WP-001", "EXECUTING", actor=PRODUCER, candidate_revision="cand-2")
    for state, kind, actor in (
            ("SPEC_REVIEW", "specification_compliance", SPEC_REVIEWER),
            ("CODE_REVIEW", "code_quality", CODE_REVIEWER),
            ("VERIFYING", "verification", VERIFIER)):
        store.transition("WP-001", state, actor="orchestrator")
        store.record_review("WP-001", kind, "cand-2", actor, "PASS")
    store.transition("WP-001", "TECH_COMPLETE", actor=PRODUCER)
    verdicts = [(e["kind"], e["verdict"], e["candidate_revision"])
                for e in store.evidence("WP-001")]
    assert ("specification_compliance", "FAIL", "cand-1") in verdicts
    assert ("specification_compliance", "PASS", "cand-2") in verdicts


def test_state_survives_process_restart(tmp_path):
    """A runtime session is not durable state."""
    db = tmp_path / "state.db"
    first = Store(db)
    first.register("WP-001", "Root", "01_FOUNDATION", 1)
    first.transition("WP-001", "READY", actor="human")
    first.close()

    second = Store(db)
    assert second.get("WP-001")["state"] == "READY"
    assert [t["to_state"] for t in second.history("WP-001")] == ["READY"]
    second.close()


def test_unknown_state_is_refused(store):
    with pytest.raises(StateError, match="unknown state"):
        store.transition("WP-001", "DONE", actor="agent")


def test_unknown_review_stage_is_refused(store):
    with pytest.raises(StateError, match="unknown review stage"):
        store.record_review("WP-001", "vibes", "cand-1", VERIFIER, "PASS")
