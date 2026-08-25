"""Runtime and model control.

The operating reality this exists for: a quota runs out mid-package. That is not
an exceptional error to be surfaced as a traceback — it is a normal state the
design has to absorb, by rebinding the work to another runtime without the role,
the authority or the task state moving with it.

Three things this layer must keep straight, because collapsing any two of them
is how a fallback quietly weakens assurance:

* **A role is not a runtime.** Rebinding an implementer from one runtime to
  another leaves it the implementer. It does not become allowed to review.
* **Availability is not eligibility.** A runtime that is up can still be
  unqualified for a role, or disqualified for *this* role on *this* package
  because it already holds another role on it.
* **A failure to run is not a failure to implement.** `QUOTA_EXHAUSTED` says
  nothing about the candidate.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent.parent / "config" / "runtimes.json"

# Every way a runtime can be unusable, kept distinct because the correct
# response differs: a rate limit is waited out, an exhausted quota is not, and a
# missing runtime is an installation problem rather than a provider problem.
STATUSES = (
    "AVAILABLE",          # usable now
    "DEGRADED",           # usable, but near a limit or slow
    "RATE_LIMITED",       # temporarily refusing; retry later is meaningful
    "QUOTA_EXHAUSTED",    # refusing until a billing period turns over
    "AUTH_FAILED",        # credentials wrong or expired
    "RUNTIME_MISSING",    # the harness is not installed here
    "PROVIDER_DOWN",      # upstream outage
    "MODEL_UNAVAILABLE",  # the runtime is fine, this model is not
    "LOCAL_SATURATED",    # local GPU or memory cannot take more work
    "UNKNOWN",            # not probed — never treated as AVAILABLE
)

USABLE = {"AVAILABLE", "DEGRADED"}

# Human overrides. `RESERVE` is the one that matters in practice: it keeps a
# scarce premium runtime for the work that actually needs it instead of letting
# routine implementation burn it.
MODES = ("NORMAL", "RESERVE", "DISABLED", "PINNED")

# What a reserved runtime may still be spent on.
RESERVE_ADMITS = ("ARCHITECTURE_CRITICAL", "SPEC_CONFLICT", "HIGH_RISK_REVIEW")

# Which role each admitting class is actually about. Reserving used to be purely
# subtractive: it kept a premium runtime out of routine work and then never
# reached it, because the binder takes the cheapest candidate and a reserved
# runtime is the dearest by definition. A control that never changes the outcome
# it names is not a control, so on the work a reserve admits, the role that work
# turns on gets first refusal of it. Only that role: upgrading the implementer
# during an architecture-critical package spends the budget on the
# highest-volume slot and changes nothing about the decision that made the work
# critical.
RESERVE_ROLES = {
    "ARCHITECTURE_CRITICAL": ("architect",),
    "SPEC_CONFLICT": ("spec_reviewer",),
    "HIGH_RISK_REVIEW": ("code_reviewer", "verifier"),
}


class NoEligibleRuntime(RuntimeError):
    """No qualified, independent, available runtime exists for a role.

    This is a first-class outcome, not a crash. Assurance does not shrink
    because the cheap option is unavailable — the package waits.
    """


@dataclass
class Runtime:
    runtime_id: str
    provider: str
    model: str
    status: str = "UNKNOWN"
    reason: str | None = None
    mode: str = "NORMAL"
    retry_after: str | None = None
    # Roles this runtime has been *measured* to be adequate for. An unqualified
    # runtime is not eligible however available it is.
    qualified_roles: list[str] = field(default_factory=list)
    # Runtimes sharing a provider account share a quota and a failure mode, and
    # two agents from the same family are not two independent opinions.
    family: str = ""
    cost_tier: int = 1          # 1 cheap … 5 premium
    local: bool = False
    notes: str = ""

    def usable(self) -> bool:
        return self.status in USABLE and self.mode != "DISABLED"

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class RuntimeBinding:
    """Which runtime serves which agent identity, for which role.

    Three separate things, and the reason they are three: the *role* carries the
    authority, the *agent identity* is who did it, and the *runtime* is merely
    what executed it. Swapping the runtime mid-task changes none of the other
    two, which is what makes a mid-package quota failure survivable.
    """
    role_id: str
    agent_id: str
    runtime_id: str
    model: str
    family: str
    reason: str
    bound_at: str

    def as_dict(self) -> dict:
        return asdict(self)


class RuntimeRegistry:
    """The live view of what can run, and what is allowed to run what."""

    def __init__(self, runtimes: list[Runtime] | None = None):
        self.runtimes: dict[str, Runtime] = {r.runtime_id: r for r in (runtimes or [])}

    @classmethod
    def load(cls, path: Path | None = None) -> "RuntimeRegistry":
        path = Path(path) if path else CONFIG
        if not path.is_file():
            return cls([])
        data = json.loads(path.read_text())
        return cls([Runtime(**r) for r in data["runtimes"]])

    def save(self, path: Path | None = None) -> Path:
        from ..state import json_dump
        path = Path(path) if path else CONFIG
        json_dump({"schema": "dume.runtimes/1",
                   "runtimes": [r.as_dict() for r in self.runtimes.values()]}, path)
        return path

    def get(self, runtime_id: str) -> Runtime:
        try:
            return self.runtimes[runtime_id]
        except KeyError:
            raise KeyError(f"no such runtime: {runtime_id!r}") from None

    # ---- human control --------------------------------------------------

    def set_status(self, runtime_id: str, status: str, reason: str | None = None,
                   retry_after: str | None = None) -> Runtime:
        if status not in STATUSES:
            raise ValueError(f"unknown runtime status: {status!r}")
        rt = self.get(runtime_id)
        rt.status, rt.reason, rt.retry_after = status, reason, retry_after
        return rt

    def set_mode(self, runtime_id: str, mode: str) -> Runtime:
        if mode not in MODES:
            raise ValueError(f"unknown mode: {mode!r}; expected one of {MODES}")
        rt = self.get(runtime_id)
        rt.mode = mode
        return rt

    # ---- eligibility ----------------------------------------------------

    def eligible(self, role_id: str, *, exclude_families: set[str] | None = None,
                 exclude_runtimes: set[str] | None = None,
                 work_class: str | None = None) -> list[Runtime]:
        """Runtimes that may take this role, cheapest adequate first.

        Four separate filters, each of which can disqualify on its own:
        available, not disabled, qualified for the role, and independent of the
        identities already bound on this package.
        """
        exclude_families = exclude_families or set()
        exclude_runtimes = exclude_runtimes or set()
        out = []
        for rt in self.runtimes.values():
            if not rt.usable():
                continue
            if rt.runtime_id in exclude_runtimes:
                continue
            if rt.family and rt.family in exclude_families:
                continue
            if role_id not in rt.qualified_roles:
                continue
            if rt.mode == "RESERVE" and work_class not in RESERVE_ADMITS:
                continue
            out.append(rt)
        # Cheapest first, then local before remote: a local model costs no quota
        # and its failure does not depend on someone else's billing period.
        return sorted(out, key=lambda r: (r.cost_tier, not r.local, r.runtime_id))

    def bind(self, role_id: str, *, already_bound: dict[str, RuntimeBinding] | None = None,
             work_class: str | None = None, independent_of: tuple[str, ...] = (),
             family_independent_of: tuple[str, ...] = (),
             agent_id: str | None = None) -> RuntimeBinding:
        """Choose a runtime for a role, or refuse.

        Identity independence is **not** a constraint on this choice. A role is
        not an agent and an agent is not a runtime: two agents with separate
        identities, separate contexts and separate personas can be served by the
        same model and still be two opinions. Conflating the two would demand as
        many providers as roles and make full independence unaffordable for no
        gain in assurance.

        What *is* constrained here is the model family, and only where a shared
        blind spot would matter: a reviewer from the same family as the
        implementer fails the same way the implementer failed, so its PASS is
        not independent evidence.

        `independent_of` is still accepted and recorded, so the binding says
        which identities must differ — the cohort guarantees that by minting one
        agent identity per role slot, and the state store enforces it on every
        verdict.
        """
        already_bound = already_bound or {}
        exclude_families = set()
        for other in family_independent_of:
            binding = already_bound.get(other)
            if binding and binding.family:
                exclude_families.add(binding.family)

        candidates = self.eligible(role_id, exclude_families=exclude_families,
                                   work_class=work_class)
        if not candidates:
            raise NoEligibleRuntime(self._explain(role_id, exclude_families,
                                                  set(), work_class))
        # Among equally eligible runtimes, prefer a family nothing else on this
        # package is using — but only for roles where independence is actually
        # the point. Spending family diversity on the orchestrator would push
        # the roles that need it onto whatever is left.
        # On work a reserve admits, the role that work turns on takes the
        # reserved runtime first; everything else keeps the cheapest-first order.
        upgraded = False
        if role_id in RESERVE_ROLES.get(work_class or "", ()):
            held = [c for c in candidates if c.mode == "RESERVE"]
            if held:
                candidates = held + [c for c in candidates if c.mode != "RESERVE"]
                upgraded = True

        if family_independent_of:
            used = {b.family for b in already_bound.values() if b.family}
            chosen = next((c for c in candidates if c.family not in used), candidates[0])
        else:
            chosen = candidates[0]
        reason = ((f"reserved for {work_class}, spent on {role_id}"
                   if upgraded and chosen.mode == "RESERVE"
                   else f"cheapest qualified runtime for {role_id}")
                  + (f"; identity must differ from {', '.join(independent_of)}"
                     if independent_of else "")
                  + (f", family-independent of {', '.join(family_independent_of)}"
                     if family_independent_of else ""))
        return RuntimeBinding(
            role_id=role_id, agent_id=agent_id or role_id,
            runtime_id=chosen.runtime_id, model=chosen.model,
            family=chosen.family, reason=reason,
            bound_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def _explain(self, role_id: str, families: set[str], runtimes: set[str],
                 work_class: str | None) -> str:
        """Say *why* nothing was eligible. `BLOCKED_RUNTIME` with no reason is
        an outage report nobody can act on."""
        lines = [f"no eligible runtime for role {role_id!r}"]
        for rt in self.runtimes.values():
            if not rt.usable():
                lines.append(f"  {rt.runtime_id}: {rt.status}"
                             + (f" ({rt.reason})" if rt.reason else ""))
            elif rt.runtime_id in runtimes:
                lines.append(f"  {rt.runtime_id}: already bound to a role this "
                             "one must be independent of")
            elif rt.family in families:
                lines.append(f"  {rt.runtime_id}: same model family "
                             f"({rt.family}) as a role this one must be "
                             "independent of")
            elif role_id not in rt.qualified_roles:
                lines.append(f"  {rt.runtime_id}: not qualified for {role_id}")
            elif rt.mode == "RESERVE":
                lines.append(f"  {rt.runtime_id}: RESERVE — admits only "
                             f"{', '.join(RESERVE_ADMITS)}, this work is "
                             f"{work_class or 'unclassified'}")
        lines.append("assurance does not shrink because a runtime is missing; "
                     "the package waits")
        return "\n".join(lines)

    # ---- reporting ------------------------------------------------------

    def table(self) -> list[dict]:
        icon = {"AVAILABLE": "🟢", "DEGRADED": "🟠", "RATE_LIMITED": "🟠",
                "QUOTA_EXHAUSTED": "🔴", "AUTH_FAILED": "🔴",
                "RUNTIME_MISSING": "⚫", "PROVIDER_DOWN": "🔴",
                "MODEL_UNAVAILABLE": "🔴", "LOCAL_SATURATED": "🟠",
                "UNKNOWN": "⚪"}
        return [{"runtime": r.runtime_id, "icon": icon.get(r.status, "⚪"),
                 "status": r.status, "mode": r.mode, "model": r.model,
                 "family": r.family, "local": r.local, "reason": r.reason,
                 "qualified_for": r.qualified_roles}
                for r in sorted(self.runtimes.values(),
                                key=lambda r: (r.cost_tier, r.runtime_id))]
