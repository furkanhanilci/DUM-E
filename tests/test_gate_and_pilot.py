"""The deterministic gate, and the end-to-end pilot that characterises it."""
import pytest

from conftest import CODE_REVIEWER, PRODUCER, SPEC_REVIEWER, VERIFIER, drive
from dume.acceptance.gate import MergeGate
from dume.control.pilot import run_all, run_case


def test_the_gate_refuses_when_it_cannot_see_the_worktree(store):
    """An unchecked assumption is not a passed check."""
    drive(store)
    result = MergeGate(store).evaluate("WP-001", "cand-1")
    assert result.verdict == "REFUSED"
    names = {c.name for c in result.failed}
    assert "worktree_clean" in names
    assert "protected_paths_untouched" in names


def test_the_gate_names_every_check_it_failed(store):
    drive(store)
    result = MergeGate(store).evaluate("WP-001", "cand-OTHER")
    assert "candidate_unchanged" in {c.name for c in result.failed}
    for check in result.checks:
        assert check.question.endswith("?"), check
        assert check.detail


def test_the_gate_refuses_a_candidate_it_was_not_asked_about(store):
    drive(store, candidate="cand-1")
    result = MergeGate(store).evaluate("WP-001", "cand-2")
    assert result.verdict == "REFUSED"


def test_the_gate_requires_all_three_questions_answered(store):
    drive(store, stop_at="reviews")
    result = MergeGate(store).evaluate("WP-001", "cand-1")
    failed = {c.name for c in result.failed}
    assert {"specification_compliance_passed", "code_quality_passed",
            "verification_passed"} <= failed


def test_the_gate_catches_a_verifier_who_already_reviewed(store):
    drive(store, stop_at="reviews")
    store.transition("WP-001", "SPEC_REVIEW", actor="orchestrator")
    store.record_review("WP-001", "specification_compliance", "cand-1",
                        SPEC_REVIEWER, "PASS")
    store.transition("WP-001", "CODE_REVIEW", actor="orchestrator")
    store.record_review("WP-001", "code_quality", "cand-1", CODE_REVIEWER, "PASS")
    store.transition("WP-001", "VERIFYING", actor="orchestrator")
    store.record_review("WP-001", "verification", "cand-1", CODE_REVIEWER, "PASS")
    result = MergeGate(store).evaluate("WP-001", "cand-1")
    assert "verification_independent" in {c.name for c in result.failed}


def test_the_gate_refuses_an_empty_required_artefact(store, tmp_path):
    drive(store)
    empty = tmp_path / "report.json"
    empty.write_text("")
    result = MergeGate(store).evaluate("WP-001", "cand-1",
                                       required_artefacts=[str(empty)])
    detail = next(c.detail for c in result.checks
                  if c.name == "required_artefacts_present")
    assert "empty" in detail


def test_the_gate_has_no_model_in_it():
    """The whole point. If this file ever imports a model client, the gate has
    become an opinion."""
    import inspect

    from dume.acceptance import gate
    source = inspect.getsource(gate)
    for forbidden in ("anthropic", "openai", "requests.post", "urllib.request",
                      "completion", "chat("):
        assert forbidden not in source, f"the gate reached for {forbidden}"


# ---- the pilot ----------------------------------------------------------

@pytest.mark.parametrize("case", ["happy_path", "implementation_wrong",
                                  "spec_review_fails",
                                  "frozen_acceptance_edited",
                                  "no_eligible_runtime"])
def test_each_pilot_case_reaches_the_outcome_it_predicts(case):
    spec = next(c for c in __import__("dume.control.pilot", fromlist=["CASES"]).CASES
                if c[0] == case)
    name, wp, inject, exhaust, expect = spec
    result = run_case(name, wp, inject=inject, exhaust=exhaust, expect=expect)
    assert result["verdict"] == expect, result["steps"]


def test_the_pilot_proves_the_failure_paths_not_only_the_happy_one():
    report = run_all()
    assert report["verdict"] == "PASS"
    outcomes = {r["case"]: r["verdict"] for r in report["results"]}
    assert outcomes["happy_path"] == "MERGE_ELIGIBLE"
    # Four distinct ways to not be merge-eligible, each reached deliberately.
    assert len({v for k, v in outcomes.items() if k != "happy_path"}) >= 2


def test_a_candidate_that_edits_frozen_acceptance_never_reaches_review():
    """Reviewing a candidate that rewrote its own criterion is wasted effort."""
    result = run_case("protected", "WP-001", inject="protected_path",
                      expect="FAILED")
    steps = {s["name"]: s["outcome"] for s in result["steps"]}
    assert steps["protected_paths"] == "FAILED"
    assert "specification_compliance" not in steps
