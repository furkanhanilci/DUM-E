"""WP-026 — moving a task to another runtime without losing it.

A quota runs out mid-package. The wrong response is to copy the failing agent's
entire conversation into the replacement: it doubles the context, carries over
whatever the first agent was confused about, and quietly destroys independence
if the replacement is a reviewer.

The right response is to hand over the *task*, not the conversation. What
survives a switch is exactly what an agent needs to continue and nothing that
would prejudice it:

* the frozen packet and its digest
* the accepted plan
* the worktree path and the current candidate commit
* which step of the plan is done and which is open
* open findings raised against it
* the role it is filling

What does not survive: the previous agent's reasoning, its dead ends, its
half-formed conclusions, and any verdict another role has already reached.

The role does not change. Only the runtime does. That distinction is what makes
a mid-package quota failure survivable rather than a restart.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .failures import classify
from .profiles import NoEligibleRuntime, RuntimeBinding, RuntimeRegistry

# Statuses that mean "this runtime cannot take the work now". A switch is
# warranted; a retry against the same runtime is not.
SWITCH_WORTHY = {"QUOTA_EXHAUSTED", "RATE_LIMITED", "AUTH_FAILED",
                 "PROVIDER_DOWN", "MODEL_UNAVAILABLE", "RUNTIME_MISSING",
                 "LOCAL_SATURATED"}


@dataclass
class TaskHandoff:
    """Everything the replacement gets. Deliberately small."""
    task_id: str
    wp_id: str
    logical_role: str
    from_binding: dict | None
    to_binding: dict | None
    reason: str
    candidate_sha: str | None
    worktree: str | None
    accepted_plan: dict = field(default_factory=dict)
    packet_sha256: str | None = None
    completed_steps: list[str] = field(default_factory=list)
    open_steps: list[str] = field(default_factory=list)
    open_findings: list[dict] = field(default_factory=list)
    artefact_refs: list[str] = field(default_factory=list)
    context_policy: str = (
        "task state only. The previous agent's conversation is deliberately "
        "excluded: it would double the context, carry over its confusions, and "
        "prejudice a role that is supposed to form its own view.")
    issued_at: str = ""

    def as_dict(self) -> dict:
        return asdict(self)

    def briefing(self) -> str:
        """What the replacement is actually told, in its own prompt."""
        lines = [
            f"# You are taking over {self.wp_id} as {self.logical_role}",
            "",
            f"The previous runtime could not continue: {self.reason}",
            "",
            "This says nothing about the work. You are not inheriting a "
            "mistake, and you are not being asked to agree with anything "
            "anyone concluded before you — you have not been shown it.",
            "",
            f"- packet digest: {self.packet_sha256 or '—'}",
            f"- worktree: {self.worktree or '—'}",
            f"- current candidate: {(self.candidate_sha or '—')[:12]}",
        ]
        if self.accepted_plan:
            lines += ["", "## the accepted plan",
                      json.dumps(self.accepted_plan, indent=2)[:1500]]
        if self.completed_steps:
            lines += ["", "## already done"] + [f"- {s}" for s in self.completed_steps]
        if self.open_steps:
            lines += ["", "## still open"] + [f"- {s}" for s in self.open_steps]
        if self.open_findings:
            lines += ["", "## open findings against this candidate"]
            lines += [f"- [{f.get('severity', '?')}] {f.get('summary', '')}"
                      for f in self.open_findings]
        return "\n".join(lines)


class SwitchRefused(RuntimeError):
    """No replacement could be bound. The package waits."""


class RuntimeSwitcher:
    """Manual switching, and policy-driven fallback that keeps the role."""

    def __init__(self, registry: RuntimeRegistry, evidence_dir: Path | None = None):
        self.registry = registry
        self.evidence_dir = Path(evidence_dir) if evidence_dir else Path("evidence")
        self.handoffs: list[TaskHandoff] = []

    # ---- manual ---------------------------------------------------------

    def switch(self, *, role: str, wp_id: str, task_id: str,
               current: RuntimeBinding | None, to_runtime: str | None = None,
               reason: str = "operator request",
               already_bound: dict | None = None,
               independent_of: tuple[str, ...] = (),
               family_independent_of: tuple[str, ...] = (),
               work_class: str | None = None,
               plan: dict | None = None, worktree: str | None = None,
               candidate: str | None = None,
               completed: list[str] | None = None,
               open_steps: list[str] | None = None,
               findings: list[dict] | None = None,
               packet_sha256: str | None = None) -> TaskHandoff:
        """Rebind a role, preserving the task and none of the conversation."""
        if to_runtime:
            target = self.registry.get(to_runtime)
            if not target.usable():
                raise SwitchRefused(
                    f"{to_runtime} is {target.status}"
                    + (f" ({target.reason})" if target.reason else "")
                    + "; a switch onto an unusable runtime is not a switch")
            if role not in target.qualified_roles:
                raise SwitchRefused(
                    f"{to_runtime} is not qualified for {role}. Availability is "
                    "not eligibility, and a forced switch onto an unqualified "
                    "runtime would lower assurance silently.")
            new = RuntimeBinding(
                role_id=role, agent_id=f"{wp_id}/{role}@{to_runtime}",
                runtime_id=target.runtime_id, model=target.model,
                family=target.family,
                reason=f"operator switch: {reason}",
                bound_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        else:
            bound = dict(already_bound or {})
            # The runtime that just failed must not be chosen again for this
            # role, so it is excluded by making it ineligible rather than by
            # hoping the ranking avoids it.
            if current:
                failed = self.registry.runtimes.get(current.runtime_id)
                previous_status = failed.status if failed else None
                if failed and failed.status not in SWITCH_WORTHY:
                    failed.status, failed.reason = "DEGRADED", reason
            try:
                new = self.registry.bind(
                    role, already_bound=bound, work_class=work_class,
                    independent_of=independent_of,
                    family_independent_of=family_independent_of,
                    agent_id=f"{wp_id}/{role}@fallback")
            except NoEligibleRuntime as exc:
                raise SwitchRefused(
                    f"{role} cannot be rebound and the package waits.\n{exc}") from None

        handoff = TaskHandoff(
            task_id=task_id, wp_id=wp_id, logical_role=role,
            from_binding=current.as_dict() if current else None,
            to_binding=new.as_dict(), reason=reason,
            candidate_sha=candidate, worktree=worktree,
            accepted_plan=plan or {}, packet_sha256=packet_sha256,
            completed_steps=list(completed or []),
            open_steps=list(open_steps or []),
            open_findings=list(findings or []),
            issued_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self.handoffs.append(handoff)
        self._record(handoff)
        return handoff

    # ---- policy ---------------------------------------------------------

    def should_switch(self, failure_class: str,
                      binding: RuntimeBinding | None) -> tuple[bool, str]:
        """Is a switch the right answer to this failure?

        Only for failures that are about the runtime. Switching after an
        implementation failure would move the work to a fresh model and call the
        same broken candidate someone else's problem.
        """
        cls = classify(failure_class)
        if cls.candidate_implicated:
            return False, (f"{failure_class} implicates the candidate; a "
                           "different runtime would inherit the same wrong work")
        if failure_class != "RUNTIME_FAILURE":
            return False, f"{failure_class} is owned by {cls.owner}, not by routing"
        if binding is None:
            return False, "nothing is bound to switch away from"
        runtime = self.registry.runtimes.get(binding.runtime_id)
        if runtime and runtime.status in SWITCH_WORTHY:
            return True, f"{binding.runtime_id} is {runtime.status}"
        return True, f"{binding.runtime_id} failed to run the work"

    def _record(self, handoff: TaskHandoff) -> Path:
        from ..state import json_dump
        path = (self.evidence_dir / handoff.wp_id / "handoffs"
                / f"{handoff.logical_role}-{len(self.handoffs)}.json")
        json_dump(handoff.as_dict(), path)
        return path
