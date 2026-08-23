"""The lifecycle controls, exercised as behaviour rather than asserted as prose.

Each test names the invariant it defends. If one of these passes while the
control is removed, the test is wrong.
"""
import pytest

from dume.state import Store, StateError


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "state.db")
    s.register("WP-001", "Root package", "01_FOUNDATION", 1)
    s.register("WP-002", "Dependent package", "01_FOUNDATION", 2, depends_on=["WP-001"])
    yield s
    s.close()


def _drive_to_tech_complete(store, wp="WP-001", producer="producer-A", rev="cand-1"):
    store.transition(wp, "READY", actor="human")
    store.transition(wp, "IN_PROGRESS", actor=producer, candidate_revision=rev)
    store.transition(wp, "TECH_COMPLETE", actor=producer)


def test_ready_requires_accepted_dependencies(store):
    """READY is a claim about dependencies, so an unaccepted one must refuse it."""
    with pytest.raises(StateError, match="hard dependencies not ACCEPTED"):
        store.transition("WP-002", "READY", actor="human")


def test_tech_complete_is_not_accepted(store):
    """I-05. There is no edge from implementation straight to acceptance."""
    store.transition("WP-001", "READY", actor="human")
    store.transition("WP-001", "IN_PROGRESS", actor="producer-A", candidate_revision="cand-1")
    with pytest.raises(StateError, match="not a permitted transition"):
        store.transition("WP-001", "ACCEPTED", actor="verifier-B")


def test_producer_cannot_accept_own_package(store):
    """I-06. The producer may not control its own acceptance."""
    _drive_to_tech_complete(store)
    store.add_evidence("WP-001", "verification", "cand-1", "verifier-B", verdict="PASS")
    with pytest.raises(StateError, match="may not accept its own package"):
        store.transition("WP-001", "ACCEPTED", actor="producer-A")


def test_producer_cannot_supply_its_own_verification(store):
    """I-06 again, one layer deeper: a bystander may not rubber-stamp
    verification the producer wrote itself."""
    _drive_to_tech_complete(store)
    store.add_evidence("WP-001", "verification", "cand-1", "producer-A", verdict="PASS")
    with pytest.raises(StateError, match="recorded by the producer"):
        store.transition("WP-001", "ACCEPTED", actor="verifier-B")


def test_acceptance_requires_verification_evidence(store):
    """I-08. A verdict with no evidence behind it is a claim, not a result."""
    _drive_to_tech_complete(store)
    with pytest.raises(StateError, match="no verification evidence"):
        store.transition("WP-001", "ACCEPTED", actor="verifier-B")


def test_stale_evidence_from_another_candidate_does_not_carry_over(store):
    """I-23. A green result from an older candidate is not evidence for a newer one."""
    _drive_to_tech_complete(store, rev="cand-1")
    store.add_evidence("WP-001", "verification", "cand-OLD", "verifier-B", verdict="PASS")
    with pytest.raises(StateError, match="no verification evidence for candidate cand-1"):
        store.transition("WP-001", "ACCEPTED", actor="verifier-B")


def test_acceptance_candidate_must_match_candidate_under_review(store):
    """A candidate that changed after review invalidates the review."""
    _drive_to_tech_complete(store, rev="cand-1")
    store.add_evidence("WP-001", "verification", "cand-2", "verifier-B", verdict="PASS")
    with pytest.raises(StateError, match="does not match the candidate under review"):
        store.transition("WP-001", "ACCEPTED", actor="verifier-B",
                         candidate_revision="cand-2")


def test_failing_verdict_does_not_accept(store):
    _drive_to_tech_complete(store)
    store.add_evidence("WP-001", "verification", "cand-1", "verifier-B", verdict="FAIL")
    with pytest.raises(StateError, match="no PASSing verification verdict"):
        store.transition("WP-001", "ACCEPTED", actor="verifier-B")


def test_open_critical_finding_blocks_acceptance(store):
    _drive_to_tech_complete(store)
    store.add_evidence("WP-001", "verification", "cand-1", "verifier-B", verdict="PASS")
    store.add_finding("WP-001", "CRITICAL", "boundary can be bypassed by symlink")
    with pytest.raises(StateError, match="open Critical/High finding"):
        store.transition("WP-001", "ACCEPTED", actor="verifier-B")


def test_happy_path_accepts_and_releases_dependent(store):
    _drive_to_tech_complete(store)
    store.add_evidence("WP-001", "verification", "cand-1", "verifier-B", verdict="PASS")
    store.transition("WP-001", "ACCEPTED", actor="verifier-B")
    assert store.get("WP-001")["state"] == "ACCEPTED"
    # The dependent package is only now permitted to be READY.
    store.transition("WP-002", "READY", actor="human")
    assert store.get("WP-002")["state"] == "READY"


def test_retry_preserves_prior_failed_evidence(store):
    """I-24. A retry adds; it never erases what went wrong the first time."""
    _drive_to_tech_complete(store)
    store.add_evidence("WP-001", "verification", "cand-1", "verifier-B", verdict="FAIL")
    store.transition("WP-001", "REJECTED", actor="verifier-B", reason="T04 failed")
    store.transition("WP-001", "IN_PROGRESS", actor="producer-A", candidate_revision="cand-2")
    store.transition("WP-001", "TECH_COMPLETE", actor="producer-A")
    store.add_evidence("WP-001", "verification", "cand-2", "verifier-B", verdict="PASS")
    store.transition("WP-001", "ACCEPTED", actor="verifier-B", candidate_revision="cand-2")
    verdicts = [e["verdict"] for e in store.evidence("WP-001")]
    assert "FAIL" in verdicts and "PASS" in verdicts


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
