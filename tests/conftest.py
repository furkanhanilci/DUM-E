import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from dume.state import Store

PRODUCER = "producer-alpha"
SPEC_REVIEWER = "spec-reviewer-beta"
CODE_REVIEWER = "code-reviewer-gamma"
VERIFIER = "verifier-delta"


def drive(store, wp="WP-001", candidate="cand-1", producer=PRODUCER,
          spec_reviewer=SPEC_REVIEWER, code_reviewer=CODE_REVIEWER,
          verifier=VERIFIER, stop_at=None):
    """Walk a package through the pipeline, stopping wherever a test wants it.

    Written once here because every lifecycle test needs the same eleven steps,
    and a test that re-types them is a test that will drift from the machine.
    """
    steps = [
        ("READY", "human"), ("PACKAGED", "human"), ("PLANNED", "architect"),
        ("EXECUTING", producer),
    ]
    for state, actor in steps:
        if stop_at == state:
            return
        store.transition(wp, state, actor=actor,
                         candidate_revision=candidate if state == "EXECUTING" else None)
    if stop_at == "reviews":
        return
    for state, kind, actor in (
            ("SPEC_REVIEW", "specification_compliance", spec_reviewer),
            ("CODE_REVIEW", "code_quality", code_reviewer),
            ("VERIFYING", "verification", verifier)):
        store.transition(wp, state, actor="orchestrator")
        store.record_review(wp, kind, candidate, actor, "PASS")
        if stop_at == state:
            return
    if stop_at == "TECH_COMPLETE":
        store.transition(wp, "TECH_COMPLETE", actor=producer)
        return
    store.transition(wp, "TECH_COMPLETE", actor=producer)
    store.transition(wp, "ACCEPTANCE_READY", actor="orchestrator")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "state.db")
    s.register("WP-001", "Root package", "01_FOUNDATION", 1)
    s.register("WP-002", "Dependent package", "01_FOUNDATION", 2, depends_on=["WP-001"])
    yield s
    s.close()
