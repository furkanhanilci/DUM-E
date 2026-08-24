"""WP-051 — how often the harness actually works.

One green run is an anecdote. A commissioning harness that succeeds sometimes
and nobody knows how often is one that will be trusted at the wrong moment, so
this repeats a live run and reports the distribution rather than the best case.

What it measures is deliberately not "did the model write good code". It is:

* how far the pipeline gets, per attempt
* which stage stops it, when it stops
* whether the failures are the same failure or different ones

The last is the interesting one. A harness that fails the same way every time
has one bug. A harness that fails a different way each time has a reliability
problem, and those need different responses.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

STAGES = ("precondition", "packet", "cohort", "runtime_binding", "plan",
          "worktree", "implement", "protected_paths",
          "specification_compliance", "code_quality", "verification",
          "tech_complete", "machine_gate")


@dataclass
class Attempt:
    index: int
    verdict: str
    reached: str
    stopped_at: str | None
    seconds: float
    detail: str = ""

    def as_dict(self) -> dict:
        return {"index": self.index, "verdict": self.verdict,
                "reached": self.reached, "stopped_at": self.stopped_at,
                "seconds": self.seconds, "detail": self.detail[:300]}


def summarise(report: dict) -> tuple[str, str | None, str]:
    """How far did this attempt get, and what stopped it?"""
    steps = report.get("steps", [])
    reached = steps[-1]["name"] if steps else "nothing"
    stopper = next((s for s in steps if s["outcome"] in {"FAILED", "BLOCKED"}), None)
    return (reached,
            stopper["name"] if stopper else None,
            stopper["detail"] if stopper else "")


def run(times: int = 5, wp_id: str = "WP-001",
        evidence_root: Path | None = None) -> dict:
    """Repeat a live run and report the spread."""
    import time

    from . import live

    evidence_root = Path(evidence_root) if evidence_root else Path("evidence/reliability")
    attempts: list[Attempt] = []
    for index in range(1, times + 1):
        started = time.time()
        report = live.run(wp_id, evidence_root=evidence_root / f"attempt-{index}")
        reached, stopped, detail = summarise(report)
        attempts.append(Attempt(
            index=index, verdict=report.get("verdict", "UNKNOWN"),
            reached=reached, stopped_at=stopped,
            seconds=round(time.time() - started, 1), detail=detail))

    verdicts = Counter(a.verdict for a in attempts)
    stoppers = Counter(a.stopped_at for a in attempts if a.stopped_at)
    green = verdicts.get("MERGE_ELIGIBLE", 0)
    # The furthest stage any attempt reached, by pipeline order rather than by
    # name, so "got to code review twice" is comparable with "got to the gate".
    def depth(name: str) -> int:
        return STAGES.index(name) if name in STAGES else -1
    return {
        "schema": "dume.reliability/1",
        "work_package": wp_id,
        "attempts": [a.as_dict() for a in attempts],
        "runs": times,
        "merge_eligible": green,
        "success_rate": round(green / times, 2) if times else 0.0,
        "verdicts": dict(verdicts),
        "stopped_at": dict(stoppers),
        "deepest_stage": max((a.reached for a in attempts), key=depth,
                             default="nothing"),
        "same_failure_every_time": len(stoppers) <= 1,
        "note": ("A harness that fails the same way every time has one bug. A "
                 "harness that fails differently each time has a reliability "
                 "problem, and those need different responses."),
    }
