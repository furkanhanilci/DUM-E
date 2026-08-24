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

import json
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
from ..review.skills import SkillsUnavailable, bundles_for_cohort
from .model_executor import ModelExecutor
from .orchestrator import Orchestrator
from .pilot import make_target_repo

ENDPOINTS = {"qwen-local": "http://127.0.0.1:8000/v1",
             "mistral-local": "http://127.0.0.1:8001/v1"}

BUZZ_URL = "http://127.0.0.1:3000"
IDENTITY_STORE = Path.home() / ".dume" / "secrets" / "buzz-identities.json"


def connect_buzz():
    """The relay, if it is there. A run without it is narrated nowhere and is
    otherwise identical — the substrate carries commentary, not authority."""
    try:
        from ..collaboration.buzz import BuzzClient, BuzzError, load_identity
    except ImportError:
        # No signature library installed. A run without the substrate is
        # narrated nowhere and is otherwise identical.
        return None
    try:
        client = BuzzClient(BUZZ_URL,
                            load_identity(IDENTITY_STORE, "dume_orchestrator"))
        client.relay_info()
        return client
    except (BuzzError, OSError):
        return None


def build_clients(bindings: dict) -> dict:
    """One client per role, pointed at whatever that role was bound to."""
    clients = {}
    for key, binding in bindings.items():
        endpoint = ENDPOINTS.get(binding.runtime_id)
        if endpoint is None:
            continue
        clients[key.split("#")[0]] = ModelClient(endpoint, model="local")
    return clients


# One bounded executable slice per package, for characterisation runs. Naming
# them here rather than inventing one per run keeps a live result comparable
# with the last one.
FOCUS = {
    # WP-001's five mandatory deliverables, stated as the executable thing that
    # produces them. Earlier this said "a module with usable_bytes(...)", the
    # spec reviewer refused it, and the reviewer was right: a bounded slice of a
    # package is not the package, and none of the five deliverables existed.
    #
    # The answer was not to tell the reviewer to lower its bar. It was to build
    # what the card actually asks for.
    "WP-001": (
        "A module `host_inventory.py` that produces WP-001's five mandatory "
        "deliverables, plus a test file for it.\n\n"
        "`collect()` returns a dict with keys: os, cpu_memory, gpu, storage, "
        "capacity_envelope, host_class.\n"
        "`classify(envelope)` returns one of HIGH_THROUGHPUT_GPU, "
        "SINGLE_GPU_CONSTRAINED, CPU_HEAVY, REMOTE_GPU_REQUIRED.\n"
        "`usable_vram_bytes(total, overhead=0.12)` returns total minus the "
        "runtime reserve, never below zero — total VRAM is not usable VRAM.\n"
        "`write_deliverables(inventory, out_dir)` writes exactly these five "
        "files and returns their paths: host_inventory.json, host_inventory.md, "
        "gpu_probe.log, disk_capacity_report.md, deployment_profile_candidate.md."
        "\n\nThe probes may return empty or absent values on a host without a "
        "GPU; the deliverables must still be produced. Do not invent a default "
        "capacity when a probe fails — record that it failed."),

    "WP-040": (
        "A module `merge_gate.py` with `evaluate(checks)` taking a list of "
        "{'name': str, 'passed': bool} and returning {'verdict': "
        "'MERGE_ELIGIBLE' or 'REFUSED', 'failed': [names]}. MERGE_ELIGIBLE only "
        "when every check passed, and an empty list is REFUSED — nothing having "
        "been checked is not the same as everything having passed."),

    "WP-042": (
        "A module `retry_policy.py` with `should_retry(failure_class, attempts, "
        "limit=3)` returning {'retry': bool, 'reason': str}. RUNTIME_FAILURE may "
        "retry the same candidate; IMPLEMENTATION_FAILURE only after a change; "
        "ACCEPTANCE_CONTRADICTION never."),
}


def run(wp_id: str = "WP-001", *, keep: bool = False,
        evidence_root: Path | None = None, focus: str | None = None) -> dict:
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
            worktrees=worktrees, evidence_dir=evidence, buzz=connect_buzz())

        # Bind first, so the executor knows which model is answering for which
        # role before any of them is asked anything.
        packet = orchestrator.build_packet(wp_id)
        from ..cohort.compiler import compile_cohort
        cohort = compile_cohort(packet)
        try:
            bindings = orchestrator.bind_cohort(cohort)
        except Exception as exc:
            return {"verdict": "BLOCKED_RUNTIME", "detail": str(exc)}

        # WP-021: the agents are held to the pinned Superpowers skills, not to
        # prose this harness invented. If the pinned install cannot be read the
        # run records that rather than quietly substituting its own wording.
        lock = json.loads((Path(__file__).resolve().parents[2] / "config"
                           / "upstream.lock.json").read_text())
        expected = next((u["pinned_revision"] for u in lock["upstreams"]
                         if u["name"] == "superpowers"), None)
        skills, skills_error = {}, None
        try:
            skills = bundles_for_cohort(cohort.role_ids(),
                                        expected_revision=expected)
        except SkillsUnavailable as exc:
            skills_error = str(exc)

        executor = ModelExecutor(worktrees=worktrees,
                                 clients=build_clients(bindings),
                                 evidence_dir=evidence, bindings=bindings,
                                 skills=skills,
                                 focus=focus or FOCUS.get(wp_id))
        report = orchestrator.run(wp_id, executor=executor)

        result = report.as_dict()
        result["schema"] = "dume.live_run/1"
        result["bindings"] = {k: v.as_dict() for k, v in bindings.items()}
        result["assurance"] = cohort.assurance_level
        result["focus"] = executor.focus
        result["skills"] = {r: b.as_dict() for r, b in skills.items()}
        result["skills_error"] = skills_error
        result["channel"] = orchestrator.channel
        result["cohort_identities"] = (orchestrator.cohort_identities.public()
                                       if orchestrator.cohort_identities else None)
        result["note"] = (
            "Real models, real worktree, real test runs, against a disposable "
            "target created and destroyed inside the run. No real target was "
            "touched."
            + (" The build was narrowed to one executable slice of the package; "
               "the packet's constraints and acceptance criteria were not. This "
               "characterises the harness — it does not complete the package."
               if executor.focus else ""))
        if keep:
            result["kept_at"] = str(root)
        store.close()
        return result
    finally:
        if not keep:
            shutil.rmtree(root, ignore_errors=True)
