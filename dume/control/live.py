"""A real commissioning run: real models, real worktree, real tests.

The synthetic pilot proves the pipeline holds. This proves it does something.
Every role here is a live model behind a bound runtime, the implementer works
only through tools scoped to its worktree, and the verdicts are the ones the
models actually returned.

The target is still disposable. Commissioning a real target is a decision with
a human in it — binding a workspace is the one action this harness refuses to
take on its own — so what a live run demonstrates is the machinery, on work that
cannot hurt anything.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from ..cohort.role_registry import ROLES
from ..packets.wp_packet_builder import PacketBuilder
from ..runtimes.client import ModelClient
from ..runtimes.profiles import NoEligibleRuntime, RuntimeRegistry
from ..state import Store, json_dump
from ..worktrees.manager import WorktreeManager
from .model_executor import ModelExecutor
from .orchestrator import Orchestrator
from .pilot import make_target_repo

ENDPOINTS = {"qwen-local": "http://127.0.0.1:8000/v1",
             "mistral-local": "http://127.0.0.1:8001/v1"}


def build_clients(bindings: dict) -> dict:
    """One client per role, pointed at whatever that role was bound to."""
    clients = {}
    for key, binding in bindings.items():
        endpoint = ENDPOINTS.get(binding.runtime_id)
        if endpoint is None:
            continue
        clients[key.split("#")[0]] = ModelClient(endpoint, model="local")
    return clients


def run(wp_id: str = "WP-001", *, keep: bool = False,
        evidence_root: Path | None = None) -> dict:
    root = Path(tempfile.mkdtemp(prefix="dume-live-"))
    evidence = Path(evidence_root) if evidence_root else root / "evidence"
    try:
        repo = make_target_repo(root)
        store = Store(root / "live.db")
        from ..catalogue import seed
        seed(store)
        store.transition(wp_id, "READY", actor="human/otonom")

        registry = RuntimeRegistry.load()
        worktrees = WorktreeManager(repo, root / "worktrees",
                                    protected_paths=["acceptance/**"])
        orchestrator = Orchestrator(
            store, packet_builder=PacketBuilder(), registry=registry,
            worktrees=worktrees, evidence_dir=evidence)

        # Bind first, so the executor knows which model is answering for which
        # role before any of them is asked anything.
        packet = orchestrator.build_packet(wp_id)
        from ..cohort.compiler import compile_cohort
        cohort = compile_cohort(packet)
        try:
            bindings = orchestrator.bind_cohort(cohort)
        except Exception as exc:
            return {"verdict": "BLOCKED_RUNTIME", "detail": str(exc)}

        executor = ModelExecutor(worktrees=worktrees,
                                 clients=build_clients(bindings),
                                 evidence_dir=evidence, bindings=bindings)
        report = orchestrator.run(wp_id, executor=executor)

        result = report.as_dict()
        result["schema"] = "dume.live_run/1"
        result["bindings"] = {k: v.as_dict() for k, v in bindings.items()}
        result["assurance"] = cohort.assurance_level
        result["note"] = ("Real models, real worktree, real test runs, against a "
                          "disposable target created and destroyed inside the "
                          "run. No real target was touched.")
        if keep:
            result["kept_at"] = str(root)
        store.close()
        return result
    finally:
        if not keep:
            shutil.rmtree(root, ignore_errors=True)
