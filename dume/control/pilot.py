"""The synthetic end-to-end commissioning pilot.

Before a harness is pointed at real work, it has to be characterised on work
that cannot hurt anything. This pilot drives the whole pipeline — packet,
cohort, runtime binding, worktree, RED/GREEN, three reviews, fresh verification,
machine gate — against a throwaway target repository, and then does it again
with faults injected, because a pipeline that has only ever succeeded has not
been shown to fail correctly.

Nothing here touches a real target. The repository is created in a temporary
directory and destroyed afterwards.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from ..packets.wp_packet_builder import PacketBuilder
from ..runtimes.profiles import Runtime, RuntimeRegistry
from ..state import Store, json_dump
from ..worktrees.manager import WorktreeManager
from .executors import SyntheticExecutor
from .orchestrator import Orchestrator

# Runtimes that exist only for the pilot. Named so that no report can mistake a
# characterisation run for a real binding.
def synthetic_registry(*, exhaust: tuple[str, ...] = ()) -> RuntimeRegistry:
    roles = ["commissioning_orchestrator", "architect", "implementer",
             "spec_reviewer", "code_reviewer", "verifier", "specialist"]
    runtimes = [
        Runtime("synthetic-local", "pilot", "deterministic-local", "AVAILABLE",
                qualified_roles=list(roles), family="synthetic-a", cost_tier=1,
                local=True, notes="pilot fixture — not a real runtime"),
        Runtime("synthetic-cross", "pilot", "deterministic-cross", "AVAILABLE",
                qualified_roles=list(roles), family="synthetic-b", cost_tier=2,
                notes="pilot fixture — a different family, so it can be an "
                      "independent reviewer"),
        Runtime("synthetic-third", "pilot", "deterministic-third", "AVAILABLE",
                qualified_roles=list(roles), family="synthetic-c", cost_tier=3,
                notes="pilot fixture — a third family, so verification can be "
                      "independent of both reviewers"),
    ]
    registry = RuntimeRegistry(runtimes)
    for runtime_id in exhaust:
        registry.set_status(runtime_id, "QUOTA_EXHAUSTED",
                            reason="pilot fault injection")
    return registry


def make_target_repo(root: Path) -> Path:
    """A disposable target repository. Never a real one."""
    repo = root / "BUILD_TARGET_FIXTURE"
    repo.mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a],
                                    capture_output=True, check=False)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "pilot@dume.local")
    run("config", "user.name", "DUM-E pilot")
    (repo / "README.md").write_text(
        "# Disposable pilot target\n\nCreated by DUM-E's synthetic pilot and "
        "destroyed with it. Not a real build target.\n")
    acceptance = repo / "acceptance"
    acceptance.mkdir()
    (acceptance / "frozen.md").write_text("AC-01 frozen by the commissioning plan\n")
    run("add", "-A")
    run("commit", "-qm", "pilot target baseline")
    return repo


def _fresh_store(root: Path, wp_id: str) -> Store:
    store = Store(root / "pilot.db")
    from ..catalogue import seed
    seed(store)
    store.transition(wp_id, "READY", actor="pilot")
    return store


def run_case(name: str, wp_id: str, *, inject: str | None = None,
             exhaust: tuple[str, ...] = (), expect: str) -> dict:
    root = Path(tempfile.mkdtemp(prefix=f"dume-pilot-{name}-"))
    try:
        repo = make_target_repo(root)
        store = _fresh_store(root, wp_id)
        worktrees = WorktreeManager(repo, root / "worktrees",
                                    protected_paths=["acceptance/**"])
        executor = SyntheticExecutor(worktrees, evidence_dir=root / "evidence",
                                     inject=inject)
        orchestrator = Orchestrator(
            store, packet_builder=PacketBuilder(),
            registry=synthetic_registry(exhaust=exhaust),
            worktrees=worktrees, evidence_dir=root / "evidence")
        report = orchestrator.run(wp_id, executor=executor)
        final_state = store.get(wp_id)["state"]
        store.close()
        return {
            "case": name, "wp_id": wp_id, "injected": inject or "none",
            "expected": expect, "verdict": report.verdict,
            "matched": report.verdict == expect,
            "final_state": final_state,
            "steps": [s.as_dict() for s in report.steps],
            "gate": report.gate,
            "cohort_assurance": (report.cohort or {}).get("assurance_level"),
            "bindings": report.bindings,
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


CASES = (
    ("happy_path", "WP-001", None, (), "MERGE_ELIGIBLE"),
    ("implementation_wrong", "WP-001", "implementation", (), "FAILED"),
    ("spec_review_fails", "WP-001", "spec_review", (), "FAILED"),
    ("frozen_acceptance_edited", "WP-001", "protected_path", (), "FAILED"),
    ("no_eligible_runtime", "WP-001", None,
     ("synthetic-local", "synthetic-cross", "synthetic-third"), "BLOCKED_RUNTIME"),
)


def run_all(out: Path | None = None) -> dict:
    results = [run_case(name, wp, inject=inject, exhaust=exhaust, expect=expect)
               for name, wp, inject, exhaust, expect in CASES]
    report = {
        "schema": "dume.synthetic_pilot/1",
        "results": results,
        "cases": len(results),
        "matched": sum(1 for r in results if r["matched"]),
        "verdict": "PASS" if all(r["matched"] for r in results) else "FAIL",
        "note": "Every case ran against a disposable target repository created "
                "and destroyed inside the run. No real target was touched.",
    }
    if out:
        json_dump(report, out)
    return report
