"""The adversarial suite must itself be trustworthy.

A scenario runner that reports PASS when it did not produce the failure
condition is worse than no runner at all, so these tests check the runner's
honesty as well as its verdicts.
"""
from dume import scenarios


def test_every_executable_scenario_passes(tmp_path):
    report = scenarios.run_all(tmp_path)
    failures = [r for r in report["results"] if r["verdict"] == "FAIL"]
    assert failures == [], failures
    assert report["verdict"] == "PASS"
    assert report["executed"] == 7


def test_a_deferred_scenario_is_never_counted_as_a_pass(tmp_path):
    report = scenarios.run_all(tmp_path)
    deferred = [r for r in report["results"] if r["verdict"] == "NOT_APPLICABLE"]
    assert len(deferred) == report["deferred"] == 29
    assert report["passed"] + report["failed"] + report["not_run"] == report["executed"]
    # Every deferred scenario names the work package that will make it runnable.
    for r in deferred:
        assert "deferred to WP-" in r["observed"]


def test_the_full_36_scenario_catalogue_is_accounted_for(tmp_path):
    """The pack ships 36 scenarios. None may quietly vanish from the report."""
    report = scenarios.run_all(tmp_path)
    assert len(report["results"]) == 36
    ids = {r["scenario"] for r in report["results"]}
    assert len(ids) == 36


def test_each_result_states_what_was_required_and_what_was_observed(tmp_path):
    for r in scenarios.run_all(tmp_path)["results"]:
        assert r["required_result"], r
        assert r["observed"], r
