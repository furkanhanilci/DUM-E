"""The Commissioning Orchestrator.

Runs one work package from READY to a gate verdict. Its authority is sequencing
and nothing else: it moves work between stages, and it is never the thing that
decides a stage passed. Every verdict in the run below comes from somewhere the
orchestrator cannot reach — a reviewer identity, a test exit code, the gate.

The flow is the one the design fixes:

    READY → packet → cohort → bindings → worktree → plan → implement
          → spec review → code review → fresh verification → machine gate

A failure at any step is classified before anything is retried, because
"the model ran out of quota" and "the implementation is wrong" are not the same
event and must not produce the same response.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..acceptance.gate import MergeGate
from ..cohort.compiler import CohortManifest, compile_cohort
from ..packets.wp_packet_builder import PacketBuilder, WPPacket
from ..runtimes.failures import classify, retry_decision
from ..runtimes.profiles import NoEligibleRuntime, RuntimeBinding, RuntimeRegistry
from ..state import StateError, json_dump
from ..worktrees.manager import ProtectedPathViolation, WorktreeManager


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Step:
    name: str
    outcome: str            # OK | BLOCKED | FAILED
    detail: str
    at: str = field(default_factory=_now)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class RunReport:
    wp_id: str
    started_at: str
    steps: list[Step] = field(default_factory=list)
    packet_sha256: str | None = None
    candidate_revision: str | None = None
    cohort: dict | None = None
    bindings: dict = field(default_factory=dict)
    gate: dict | None = None
    verdict: str = "INCOMPLETE"

    def as_dict(self) -> dict:
        d = asdict(self)
        d["steps"] = [s.as_dict() for s in self.steps]
        return d


class BlockedRuntime(RuntimeError):
    """The run stopped because no qualified independent runtime was available.

    A distinct outcome from failure. Nothing is wrong with the candidate, and
    nothing about the required assurance changes — the package waits.
    """


class Orchestrator:
    def __init__(self, store, *, packet_builder: PacketBuilder,
                 registry: RuntimeRegistry, worktrees: WorktreeManager | None = None,
                 evidence_dir: Path | None = None):
        self.store = store
        self.packets = packet_builder
        self.registry = registry
        self.worktrees = worktrees
        self.evidence_dir = Path(evidence_dir) if evidence_dir else Path("evidence")
        self.gate = MergeGate(store, worktrees)

    # ---- steps ----------------------------------------------------------

    def build_packet(self, wp_id: str) -> WPPacket:
        states = {r["wp_id"]: dict(r) for r in self.store.all_wps()}
        packet = self.packets.build(wp_id, dependency_states=states)
        out = self.evidence_dir / wp_id
        self.packets.write(packet, out)
        return packet

    def bind_cohort(self, cohort: CohortManifest, work_class: str | None = None
                    ) -> dict[str, RuntimeBinding]:
        """Bind every role to a runtime, or refuse the whole cohort.

        Bound in dependency order so that the independence constraints of the
        later roles can actually see what the earlier ones took.
        """
        from ..cohort.role_registry import ROLES
        bindings: dict[str, RuntimeBinding] = {}
        order = ["commissioning_orchestrator", "architect", "implementer",
                 "spec_reviewer", "code_reviewer", "verifier", "specialist"]
        slots = sorted(cohort.roles, key=lambda s: order.index(s.role_id)
                       if s.role_id in order else 99)
        for slot in slots:
            if not ROLES[slot.role_id].needs_runtime:
                # The harness performs this role. Binding a model to it would
                # add a voice with no vote and a quota bill.
                continue
            key = slot.role_id
            if key in bindings:
                # A second slot of the same role — two implementers, a second
                # verifier — is a second agent identity, never a reused one.
                key = f"{slot.role_id}#{sum(1 for k in bindings if k.startswith(slot.role_id)) + 1}"
            try:
                bindings[key] = self.registry.bind(
                    slot.role_id, already_bound=bindings, work_class=work_class,
                    independent_of=ROLES[slot.role_id].independent_of,
                    family_independent_of=ROLES[slot.role_id].family_independent_of,
                    agent_id=f"{cohort.wp_id}/{key}")
            except NoEligibleRuntime as exc:
                raise BlockedRuntime(f"{cohort.wp_id}: {exc}") from None
        return bindings

    # ---- the run --------------------------------------------------------

    def run(self, wp_id: str, *, executor, actor: str = "commissioning_orchestrator",
            work_class: str | None = None) -> RunReport:
        """Drive one package. `executor` performs the work each role represents.

        The executor is injected rather than built in, because what actually
        writes the code — a model behind a runtime, or a deterministic local
        stand-in during a synthetic pilot — is not the orchestrator's concern.
        What the orchestrator guarantees is the sequence, the independence and
        the evidence, and those hold whichever executor is supplied.
        """
        report = RunReport(wp_id=wp_id, started_at=_now())

        def step(name: str, outcome: str, detail: str) -> None:
            report.steps.append(Step(name, outcome, detail))

        row = self.store.get(wp_id)
        if row["state"] != "READY":
            unmet = self.store.unmet_dependencies(wp_id)
            step("precondition", "BLOCKED",
                 f"state is {row['state']}, not READY"
                 + (f"; waiting on {', '.join(unmet)}" if unmet else ""))
            report.verdict = "BLOCKED"
            return report
        step("precondition", "OK", "package is READY and dependencies are ACCEPTED")

        # 1. Packet — mechanical, frozen, digested.
        packet = self.build_packet(wp_id)
        report.packet_sha256 = packet.packet_sha256
        self.store.transition(wp_id, "PACKAGED", actor=actor,
                              reason=f"packet {packet.packet_sha256[:12]}")
        step("packet", "OK",
             f"{len(packet.sections)} frozen sections, "
             f"{len(packet.dependencies)} dependencies, "
             f"{len(packet.deliverables)} deliverables, digest "
             f"{packet.packet_sha256[:12]}")

        # 2. Cohort — derived from the packet, not from an adjective.
        cohort = compile_cohort(packet)
        report.cohort = cohort.as_dict()
        step("cohort", "OK",
             f"{cohort.assurance_level} assurance, roles: "
             f"{', '.join(cohort.role_ids())}"
             + (f"; specialists: {', '.join(s['specialist'] for s in cohort.specialists)}"
                if cohort.specialists else ""))

        # 3. Runtime binding — or an honest stop.
        try:
            bindings = self.bind_cohort(cohort, work_class=work_class)
        except BlockedRuntime as exc:
            step("runtime_binding", "BLOCKED", str(exc))
            self.store.transition(wp_id, "BLOCKED", actor=actor,
                                  reason="BLOCKED_RUNTIME")
            report.verdict = "BLOCKED_RUNTIME"
            self._write(report)
            return report
        report.bindings = {k: v.as_dict() for k, v in bindings.items()}
        step("runtime_binding", "OK",
             "; ".join(f"{k}→{v.runtime_id}({v.family})" for k, v in bindings.items()))

        # 4. Plan.
        self.store.transition(wp_id, "PLANNED",
                              actor=bindings["architect"].agent_id,
                              reason="implementation plan accepted")
        plan = executor.plan(packet, cohort)
        step("plan", "OK", plan.get("summary", "plan produced"))

        # 5. Worktree + implementation.
        worktree = None
        try:
            worktree = executor.prepare_worktree(packet)
            step("worktree", "OK",
                 f"{worktree.branch} off {worktree.base_revision[:12]}")
        except Exception as exc:
            step("worktree", "FAILED", f"{type(exc).__name__}: {exc}")
            return self._fail(report, wp_id, "HARNESS_FAILURE", actor, str(exc))

        producer = bindings["implementer"].agent_id
        self.store.transition(wp_id, "EXECUTING", actor=producer,
                              reason="implementation started")
        try:
            result = executor.implement(packet, plan, worktree)
        except Exception as exc:
            step("implement", "FAILED", f"{type(exc).__name__}: {exc}")
            return self._fail(report, wp_id, "IMPLEMENTATION_FAILURE", actor, str(exc))

        candidate = result["candidate_revision"]
        report.candidate_revision = candidate
        # EXECUTING is already the current state, so the candidate is recorded
        # directly rather than by inventing a self-transition.
        self.store.set_candidate(wp_id, candidate)
        step("implement", "OK",
             f"candidate {candidate[:12]}, RED→GREEN evidence: "
             f"{result.get('discipline', 'not recorded')}")

        # 6. Protected paths — checked before anyone reviews anything, because
        # a candidate that edited its own acceptance is not worth reviewing.
        try:
            diff = self.worktrees.assert_protected_paths_untouched(worktree, candidate)
            step("protected_paths", "OK",
                 f"{len(diff.files)} file(s) changed, none protected")
        except ProtectedPathViolation as exc:
            step("protected_paths", "FAILED", str(exc))
            self.store.add_finding(wp_id, "CRITICAL", str(exc))
            return self._fail(report, wp_id, "IMPLEMENTATION_FAILURE", actor, str(exc))

        # 7–9. The three questions, asked by three identities.
        stages = (("SPEC_REVIEW", "specification_compliance", "spec_reviewer"),
                  ("CODE_REVIEW", "code_quality", "code_reviewer"),
                  ("VERIFYING", "verification", "verifier"))
        for state, kind, role_key in stages:
            self.store.transition(wp_id, state, actor=actor)
            reviewer = bindings[role_key].agent_id
            verdict = executor.review(kind, packet, worktree, candidate)
            artefact = verdict.get("artefact")
            try:
                self.store.record_review(wp_id, kind, candidate, reviewer,
                                         verdict["verdict"],
                                         artefact_path=artefact,
                                         detail=verdict.get("detail"))
            except StateError as exc:
                step(kind, "FAILED", str(exc))
                return self._fail(report, wp_id, "HARNESS_FAILURE", actor, str(exc))
            step(kind, "OK" if verdict["verdict"] == "PASS" else "FAILED",
                 f"{reviewer}: {verdict['verdict']} — {verdict.get('detail', '')}")
            if verdict["verdict"] != "PASS":
                for finding in verdict.get("findings", []):
                    self.store.add_finding(wp_id, finding.get("severity", "HIGH"),
                                           finding["summary"])
                return self._fail(report, wp_id,
                                  verdict.get("failure_class", "IMPLEMENTATION_FAILURE"),
                                  actor, verdict.get("detail", ""))

        self.store.transition(wp_id, "TECH_COMPLETE", actor=producer,
                              candidate_revision=candidate,
                              reason="all three stages passed on this candidate")
        step("tech_complete", "OK", f"candidate {candidate[:12]}")

        # 10. The machine gate.
        self.store.transition(wp_id, "ACCEPTANCE_READY", actor=actor)
        gate = self.gate.evaluate(
            wp_id, candidate, worktree=worktree,
            packet_sha256=packet.packet_sha256,
            recorded_packet_sha256=report.packet_sha256,
            frozen_digests=result.get("frozen_digests"),
            required_artefacts=result.get("required_artefacts"))
        report.gate = gate.as_dict()
        step("machine_gate", "OK" if gate.verdict == "MERGE_ELIGIBLE" else "FAILED",
             gate.verdict + (
                 "" if gate.verdict == "MERGE_ELIGIBLE"
                 else ": " + "; ".join(f"{c.name} — {c.detail}" for c in gate.failed)))

        report.verdict = gate.verdict
        self._write(report)
        return report

    # ---- failure --------------------------------------------------------

    def _fail(self, report: RunReport, wp_id: str, failure_class: str,
              actor: str, detail: str) -> RunReport:
        cls = classify(failure_class)
        attempts = sum(1 for t in self.store.history(wp_id) if t["to_state"] == "RETRY")
        decision = retry_decision(failure_class, attempts)
        self.store.transition(wp_id, "FAILED", actor=actor,
                              reason=f"{failure_class}: {detail[:200]}")
        report.steps.append(Step(
            "failure_classification", "OK",
            f"{failure_class} — candidate implicated: {cls.candidate_implicated}; "
            f"owner: {cls.owner}; {decision['reason']}"))
        if decision["retry"]:
            self.store.transition(wp_id, "RETRY", actor=actor, reason=failure_class)
        report.verdict = "FAILED"
        self._write(report)
        return report

    def _write(self, report: RunReport) -> Path:
        path = self.evidence_dir / report.wp_id / "run_report.json"
        json_dump(report.as_dict(), path)
        return path
