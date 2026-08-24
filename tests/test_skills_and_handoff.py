"""WP-021 and WP-026: the discipline the agents get, and surviving a switch."""
import json

import pytest

from dume.review import skills as sk
from dume.runtimes.handoff import (SWITCH_WORTHY, RuntimeSwitcher, SwitchRefused,
                                   TaskHandoff)
from dume.runtimes.profiles import Runtime, RuntimeRegistry

pinned = json.load(open("config/upstream.lock.json"))
EXPECTED = next(u["pinned_revision"] for u in pinned["upstreams"]
                if u["name"] == "superpowers")


# ---- WP-021: the agents are held to a pinned artefact ------------------

def test_the_installed_revision_matches_the_lock():
    assert sk.installed_revision() == EXPECTED


def test_a_drifted_install_refuses_rather_than_running_unpinned():
    """Evidence produced under an unrecorded discipline describes a version
    nobody can look up."""
    with pytest.raises(sk.SkillsUnavailable, match="the lock pins"):
        sk.bundle_for("implementer", expected_revision="0" * 40)


def test_every_role_that_does_work_has_a_bundle():
    from dume.cohort.role_registry import ROLES
    for role, spec in ROLES.items():
        if spec.needs_runtime:
            assert role in sk.ROLE_BUNDLES, role


def test_the_primary_skill_goes_in_whole_not_summarised():
    """A skill is an instruction; a summary of an instruction is a different
    instruction."""
    bundle = sk.bundle_for("implementer", expected_revision=EXPECTED)
    primary = next(s for s in bundle.skills if s.primary)
    assert primary.name == "test-driven-development"
    assert not primary.truncated, "the primary skill was cut"
    assert "Write the test first" in bundle.text


def test_each_injected_skill_carries_a_digest():
    for skill in sk.bundle_for("verifier").skills:
        assert len(skill.sha256) == 64
        assert skill.path.endswith("SKILL.md")


def test_the_bundle_says_the_skill_wins_on_method():
    text = sk.bundle_for("code_reviewer").text
    assert "not advice from this harness" in text
    assert "it wins" in text


def test_a_bundle_stays_inside_its_budget():
    """A skill that crowds the frozen packet out of the window has replaced the
    requirement with advice about how to meet it."""
    for role in sk.ROLE_BUNDLES:
        assert len(sk.bundle_for(role).text) < 14000, role


def test_injection_is_never_reported_as_obedience():
    import inspect
    source = inspect.getsource(sk)
    assert "does not claim the skill was obeyed" in source


# ---- WP-026: a switch keeps the role and drops the conversation --------

def _registry():
    roles = ["implementer", "spec_reviewer", "verifier"]
    return RuntimeRegistry([
        Runtime("a", "p", "m", "AVAILABLE", qualified_roles=roles, family="A"),
        Runtime("b", "p", "m", "AVAILABLE", qualified_roles=roles, family="B"),
    ])


def test_a_switch_keeps_the_role_and_changes_only_the_runtime(tmp_path):
    registry = _registry()
    switcher = RuntimeSwitcher(registry, tmp_path)
    current = registry.bind("implementer", agent_id="wp/implementer")
    registry.set_status(current.runtime_id, "QUOTA_EXHAUSTED", reason="gone")
    handoff = switcher.switch(role="implementer", wp_id="WP-001", task_id="t",
                              current=current, reason="quota")
    assert handoff.logical_role == "implementer"
    assert handoff.to_binding["runtime_id"] != current.runtime_id


def test_the_replacement_is_given_the_task_and_not_the_conversation(tmp_path):
    handoff = TaskHandoff(
        task_id="t", wp_id="WP-001", logical_role="implementer",
        from_binding={"runtime_id": "a"}, to_binding={"runtime_id": "b"},
        reason="quota exhausted", candidate_sha="abc123def456",
        worktree="/tmp/wt", accepted_plan={"summary": "build X"},
        completed_steps=["a"], open_steps=["b"],
        open_findings=[{"severity": "HIGH", "summary": "missing deliverable"}])
    briefing = handoff.briefing()
    assert "abc123def456"[:12] in briefing
    assert "build X" in briefing and "already done" in briefing
    assert "missing deliverable" in briefing
    assert "not been shown it" in briefing
    assert "conversation" in handoff.context_policy


def test_a_switch_is_refused_onto_an_unqualified_runtime(tmp_path):
    """Availability is not eligibility, and a forced switch would lower
    assurance silently."""
    registry = RuntimeRegistry([
        Runtime("a", "p", "m", "AVAILABLE", qualified_roles=["implementer"], family="A"),
        Runtime("b", "p", "m", "AVAILABLE", qualified_roles=[], family="B")])
    switcher = RuntimeSwitcher(registry, tmp_path)
    with pytest.raises(SwitchRefused, match="not qualified"):
        switcher.switch(role="implementer", wp_id="W", task_id="t",
                        current=None, to_runtime="b")


def test_a_switch_is_refused_onto_an_unusable_runtime(tmp_path):
    registry = _registry()
    registry.set_status("b", "AUTH_FAILED", reason="token expired")
    switcher = RuntimeSwitcher(registry, tmp_path)
    with pytest.raises(SwitchRefused, match="AUTH_FAILED"):
        switcher.switch(role="implementer", wp_id="W", task_id="t",
                        current=None, to_runtime="b")


def test_when_nothing_can_be_rebound_the_package_waits(tmp_path):
    registry = _registry()
    for runtime in ("a", "b"):
        registry.set_status(runtime, "QUOTA_EXHAUSTED", reason="gone")
    switcher = RuntimeSwitcher(registry, tmp_path)
    with pytest.raises(SwitchRefused, match="the package waits"):
        switcher.switch(role="implementer", wp_id="W", task_id="t", current=None)


@pytest.mark.parametrize("failure,expected", [
    ("RUNTIME_FAILURE", True),
    ("IMPLEMENTATION_FAILURE", False),
    ("SPEC_MISINTERPRETATION", False),
    ("ACCEPTANCE_CONTRADICTION", False),
])
def test_switching_only_answers_failures_that_are_about_the_runtime(
        tmp_path, failure, expected):
    """Switching after an implementation failure moves the work to a fresh model
    and calls the same broken candidate someone else's problem."""
    registry = _registry()
    switcher = RuntimeSwitcher(registry, tmp_path)
    binding = registry.bind("implementer", agent_id="x")
    assert switcher.should_switch(failure, binding)[0] is expected


def test_every_switch_worthy_status_is_a_real_status():
    from dume.runtimes.profiles import STATUSES
    assert SWITCH_WORTHY <= set(STATUSES)
    assert "AVAILABLE" not in SWITCH_WORTHY


def test_the_foundation_still_runs_with_nothing_installed():
    """ADR-0001. Wiring Buzz into the orchestrator briefly made the whole CLI
    require a signature library at import time, which would have meant the
    commands that exist to characterise a bare host could not run on one."""
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    # The system interpreter, deliberately: the virtualenv has coincurve.
    result = subprocess.run(
        ["/usr/bin/python3", "-m", "dume.cli", "skills"],
        cwd=repo, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-600:]
    assert "ModuleNotFoundError" not in result.stderr


def test_buzz_is_not_imported_at_orchestrator_module_scope():
    from pathlib import Path
    source = (Path(__file__).resolve().parent.parent / "dume" / "control"
              / "orchestrator.py").read_text()
    header = source.split("class Orchestrator")[0]
    assert "from ..collaboration" not in header, \
        "the collaboration layer must be imported lazily; it is optional"
