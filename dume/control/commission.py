"""Commissioning real work, against the real state.

`live.run` is a rehearsal: it builds a synthetic target repository, seeds a
fresh store in a temporary directory, and throws both away. That is the right
shape for proving the machinery works with live models, and the wrong shape for
doing anything — a run that cannot advance the state cannot finish a package,
which is why WP-001 sat at EXECUTING through every successful rehearsal.

This is the other one. The store is the deployment's own, the target repository
is the workspace bound to BUILD_TARGET, and the evidence lands under the
package's own directory. What a run does here is kept.

The specification is mounted read-only and stays that way. A harness able to
edit the requirement it is being measured against is not being measured, and
the boundary refuses the write rather than trusting the agents not to try.
"""
from __future__ import annotations

import os

import json
import time
from pathlib import Path

from ..cohort.compiler import compile_cohort
from ..config import load as load_config
from ..packets.wp_packet_builder import PacketBuilder
from ..review.skills import SkillsUnavailable, bundles_for_cohort
from ..runtimes.profiles import RuntimeRegistry
from ..state import Store
from ..worktrees.manager import WorktreeManager
from .model_executor import ModelExecutor
from .orchestrator import Orchestrator

ROOT = Path(__file__).resolve().parents[2]


class NotCommissionable(RuntimeError):
    """The deployment cannot commission this package yet, and why.

    Raised rather than improvised around: an unbound workspace or a package in
    the wrong state is a fact about the deployment, and a run that invents a
    directory to write into produces a candidate nobody can find again.
    """


LOCK = ROOT / "state" / "commissioning.pid"


def _lock_holder() -> int | None:
    """The pid of a live run, or None.

    Written on the way in and removed on the way out, so a file that outlives
    its process is exactly the case this exists to recognise. `os.kill(pid, 0)`
    asks the kernel rather than matching a command line — matching one caught
    the waiting shells that were looking for it, twice.
    """
    try:
        pid = int(LOCK.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        # Alive and owned by somebody else. Still alive.
        return pid
    return pid


def target_repo() -> Path:
    """The repository DUM-E builds into."""
    workspaces = load_config()["workspaces"]
    target = workspaces.get("BUILD_TARGET") or {}
    if not target.get("bound") or not target.get("path"):
        raise NotCommissionable(
            "BUILD_TARGET is not bound. A package needing it is BLOCKED "
            "rather than improvised — bind it in config/dume.config.json.")
    path = Path(target["path"])
    if not (path / ".git").is_dir():
        raise NotCommissionable(
            f"{path} is not a git repository. The harness works in worktrees "
            "cut from one, so there would be nothing to cut.")
    return path


def run(wp_id: str, *, focus: str | None = None,
        store_path: Path | None = None) -> dict:
    """Commission one work package. The state it moves is the real one."""
    from .live import build_clients, connect_buzz

    repo = target_repo()
    store = Store(store_path or ROOT / "state" / "dume.db")
    evidence = ROOT / "evidence"
    started = time.monotonic()

    try:
        row = store.get(wp_id)
    except Exception as exc:
        store.close()
        raise NotCommissionable(f"no such work package: {wp_id}") from exc

    # Said plainly rather than discovered halfway through. A run that starts
    # from the wrong state fails at a stage that has nothing to do with the
    # reason, and the report then blames the wrong thing.
    # READY for a fresh package, PLANNED for one that has been retried — the
    # same two the orchestrator starts from. Naming a third here would produce
    # a run that this module admits and the orchestrator then refuses, at a
    # stage with nothing to do with the reason.
    # Asking to commission a FAILED package is what a retry is. The store
    # already spells the route — FAILED -> RETRY -> PLANNED — so walk it here
    # rather than making the operator type two transitions to say the one
    # thing they meant. Every hop is still recorded, and the reason names who
    # asked for it.
    # A run that was killed leaves the package EXECUTING and nothing ever puts
    # it back: the harness only writes that state on the way in, and the way
    # out is written by the run that is no longer alive. The lock says whether
    # a run actually holds it — a stale one is recovered, a live one is
    # refused, and the difference is a pid rather than a guess.
    if row["state"] == "EXECUTING":
        holder = _lock_holder()
        if holder is not None:
            store.close()
            raise NotCommissionable(
                f"{wp_id} is EXECUTING and process {holder} holds the run. "
                "Wait for it, or stop it first.")
        store.transition(wp_id, "FAILED", actor="operator",
                         reason="a previous run did not finish; no process holds it")
        row = store.get(wp_id)

    if row["state"] == "FAILED":
        for to_state in ("RETRY", "PLANNED"):
            store.transition(wp_id, to_state, actor="operator",
                             reason=f"recommissioned after a failed run")
        row = store.get(wp_id)

    if row["state"] not in ("READY", "PLANNED"):
        unmet = store.unmet_dependencies(wp_id)
        store.close()
        raise NotCommissionable(
            f"{wp_id} is {row['state']}; a run starts from READY or PLANNED."
            + (f" It waits on {', '.join(unmet)}." if unmet else ""))

    registry = RuntimeRegistry.load()
    worktrees = WorktreeManager(
        repo, ROOT / "worktrees",
        # What no run may touch inside the target, whatever it is asked to do.
        protected_paths=["acceptance/**", ".git/**"])
    orchestrator = Orchestrator(
        store, packet_builder=PacketBuilder(), registry=registry,
        worktrees=worktrees, evidence_dir=evidence, buzz=connect_buzz())

    try:
        packet = orchestrator.build_packet(wp_id)
        cohort = compile_cohort(packet)
        bindings = orchestrator.bind_cohort(cohort)

        # The agents are held to the pinned discipline, not to prose this
        # harness invented. An unreadable install is recorded, not substituted.
        lock = json.loads((ROOT / "config" / "upstream.lock.json").read_text())
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
                                 skills=skills, focus=focus)
        # Held for exactly as long as the run. Written here rather than at the
        # gate above so a refusal never leaves a lock behind that the next
        # invocation would have to reason about.
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        LOCK.write_text(str(os.getpid()))
        try:
            report = orchestrator.run(wp_id, executor=executor)
        finally:
            LOCK.unlink(missing_ok=True)
        result = report.as_dict()
        result["schema"] = "dume.commission/1"
        result["assurance"] = cohort.assurance_level
        result["skills_error"] = skills_error
        result["bindings"] = {k: v.as_dict() for k, v in bindings.items()}
    finally:
        store.close()

    result["seconds"] = round(time.monotonic() - started, 1)
    result["target"] = str(repo)
    result["channel"] = orchestrator.channel
    return result
