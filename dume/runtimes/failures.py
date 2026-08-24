"""Why something failed, kept separate from what failed.

"Retry" is not a diagnosis. A quota that ran out and a test that genuinely fails
both stop the work, and treating them the same way means either burning retries
on a wall or quietly re-running a broken implementation until it passes by luck.

The class decides three things: whether a retry can help, whether the candidate
is implicated, and who has to act.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class FailureClass:
    name: str
    meaning: str
    candidate_implicated: bool
    retry_helps: bool
    owner: str
    # Retrying the identical thing is only sensible for transient conditions.
    requires_change_before_retry: bool = True


CLASSES: dict[str, FailureClass] = {c.name: c for c in (
    FailureClass("IMPLEMENTATION_FAILURE",
                 "The candidate does not do what the package requires.",
                 True, True, "implementer"),
    FailureClass("SPEC_MISINTERPRETATION",
                 "The candidate implements something other than the requirement.",
                 True, True, "architect"),
    FailureClass("TEST_INFRA_FAILURE",
                 "The test harness failed, not the candidate.",
                 False, True, "platform"),
    FailureClass("DEPENDENCY_FAILURE",
                 "A hard dependency's output is missing or wrong.",
                 False, False, "orchestrator"),
    FailureClass("RUNTIME_FAILURE",
                 "The model or provider could not run the work: quota, rate "
                 "limit, auth, outage. Says nothing about the candidate.",
                 False, True, "runtime_control",
                 requires_change_before_retry=False),
    FailureClass("HARNESS_FAILURE",
                 "DUM-E itself malfunctioned.",
                 False, True, "platform"),
    FailureClass("UPSTREAM_FAILURE",
                 "An external component changed or broke under a pinned use.",
                 False, False, "platform"),
    FailureClass("ARCHITECTURE_CONFLICT",
                 "The requirement cannot be satisfied without violating a "
                 "decision the package may not overturn.",
                 False, False, "human_commander"),
    FailureClass("ACCEPTANCE_CONTRADICTION",
                 "Two frozen requirements cannot both hold. Escalate; never "
                 "edit a criterion to unblock.",
                 False, False, "human_commander"),
)}

# Failures where retrying is the wrong instinct: nothing the implementer does
# will change the answer.
ESCALATE_IMMEDIATELY = tuple(
    name for name, c in CLASSES.items() if not c.retry_helps)


class UnclassifiedFailure(RuntimeError):
    """A failure was reported without a class. That is not a report."""


def classify(name: str) -> FailureClass:
    try:
        return CLASSES[name]
    except KeyError:
        raise UnclassifiedFailure(
            f"{name!r} is not a failure class; expected one of "
            + ", ".join(sorted(CLASSES))) from None


def retry_decision(name: str, attempts: int, limit: int = 3) -> dict:
    """Whether to retry, and if not, who is now holding the problem."""
    cls = classify(name)
    if not cls.retry_helps:
        return {"retry": False, "escalate_to": cls.owner,
                "reason": f"{name}: {cls.meaning} Retrying cannot change this."}
    if attempts >= limit:
        return {"retry": False, "escalate_to": cls.owner,
                "reason": f"{name}: {attempts} attempts reached the limit of "
                          f"{limit}; repeating it again is not new information."}
    return {"retry": True, "escalate_to": None,
            "reason": f"{name}: retry {attempts + 1} of {limit}"
                      + ("; the candidate must change first"
                         if cls.requires_change_before_retry
                         else "; the condition is transient, the same candidate "
                              "may be re-run")}
