"""The deterministic merge-eligibility gate.

A reviewer saying PASS is an opinion. This is the part that is not an opinion.

Nothing in this file calls a model, and nothing in it accepts an argument that
means "trust me". Every check reads a fact — a revision, a digest, a row, a
file — and every failure names the fact that was missing. The gate can only say
MERGE_ELIGIBLE when all of them hold, and a gate that can be talked into saying
it is not a gate.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Check:
    name: str
    question: str
    passed: bool
    detail: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class GateResult:
    wp_id: str
    candidate_revision: str
    verdict: str                      # MERGE_ELIGIBLE | REFUSED
    checks: list[Check] = field(default_factory=list)
    evaluated_at: str = ""

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    def as_dict(self) -> dict:
        return {"schema": "dume.merge_eligibility/1", "wp_id": self.wp_id,
                "candidate_revision": self.candidate_revision,
                "verdict": self.verdict, "evaluated_at": self.evaluated_at,
                "checks": [c.as_dict() for c in self.checks],
                "failed_checks": [c.name for c in self.failed]}


class MergeGate:
    """Evaluates merge eligibility from recorded facts only."""

    def __init__(self, store, worktree_manager=None):
        self.store = store
        self.worktrees = worktree_manager

    def evaluate(self, wp_id: str, candidate_revision: str, *,
                 worktree=None, packet_sha256: str | None = None,
                 recorded_packet_sha256: str | None = None,
                 frozen_digests: dict[str, str] | None = None,
                 required_artefacts: list[str] | None = None) -> GateResult:
        row = self.store.get(wp_id)
        checks: list[Check] = []

        def check(name: str, question: str, passed: bool, detail: str) -> None:
            checks.append(Check(name, question, bool(passed), detail))

        # 1. The candidate under review is the candidate being gated.
        recorded = row["candidate_revision"]
        check("candidate_unchanged",
              "Is this the exact candidate that was reviewed?",
              recorded == candidate_revision,
              f"state records {recorded or '—'}, gate was asked about "
              f"{candidate_revision}")

        # 2. The worktree has nothing uncommitted. A candidate is a revision.
        if worktree is not None and self.worktrees is not None:
            dirty = self.worktrees.is_dirty(worktree)
            check("worktree_clean",
                  "Is every change committed into the candidate?",
                  not dirty,
                  "uncommitted changes present — they are not in the candidate "
                  "and were not reviewed" if dirty else "working tree clean")
        else:
            check("worktree_clean",
                  "Is every change committed into the candidate?",
                  False, "no worktree was supplied; this cannot be assumed")

        # 3–5. Each review stage passed, on this candidate, by an independent
        # identity. Three questions, three answers, three actors.
        from ..state.store import REVIEW_STAGES
        actors: dict[str, str] = {}
        for _state, kind, question in REVIEW_STAGES:
            passing = [e for e in self.store.evidence(wp_id, candidate_revision, kind)
                       if e["verdict"] == "PASS"]
            producer = row["producer_actor"]
            independent = [e for e in passing if e["actor"] != producer]
            check(f"{kind}_passed", question, bool(independent),
                  f"{len(passing)} PASS record(s), {len(independent)} from an "
                  f"identity other than the producer {producer or '—'}")
            if independent:
                actors[kind] = independent[-1]["actor"]

        # 6. Verification was independent of the reviews that preceded it.
        verifier = actors.get("verification")
        overlap = [k for k in ("specification_compliance", "code_quality")
                   if verifier and actors.get(k) == verifier]
        check("verification_independent",
              "Did the verifier avoid checking its own earlier conclusion?",
              verifier is not None and not overlap,
              f"verifier {verifier or '—'}"
              + (f" also performed {', '.join(overlap)}" if overlap
                 else ", distinct from both reviewers" if verifier
                 else " — no independent verification recorded"))

        # 7. The packet the work was done against is the packet that was frozen.
        if packet_sha256 is not None and recorded_packet_sha256 is not None:
            check("packet_unchanged",
                  "Was the work done against the frozen packet?",
                  packet_sha256 == recorded_packet_sha256,
                  f"packet digest {packet_sha256[:12]} vs recorded "
                  f"{recorded_packet_sha256[:12]}")
        else:
            check("packet_unchanged",
                  "Was the work done against the frozen packet?",
                  False, "no packet digest was supplied to compare")

        # 8. Protected paths untouched.
        if worktree is not None and self.worktrees is not None:
            report = self.worktrees.diff(worktree, candidate_revision)
            check("protected_paths_untouched",
                  "Did the candidate stay out of the paths it may not touch?",
                  not report.violations,
                  "; ".join(f"{v['path']} matched {v['pattern']}"
                            for v in report.violations) or
                  f"{len(report.files)} file(s) changed, none protected")
        else:
            check("protected_paths_untouched",
                  "Did the candidate stay out of the paths it may not touch?",
                  False, "no worktree was supplied; this cannot be assumed")

        # 9. Frozen acceptance files are byte-identical. A path rule can be
        # walked around by a rename; a digest cannot.
        if frozen_digests:
            drift = (self.worktrees.frozen_files_unchanged(
                worktree, frozen_digests, candidate_revision)
                if worktree is not None and self.worktrees is not None else
                [{"path": p, "problem": "UNCHECKED"} for p in frozen_digests])
            check("frozen_acceptance_unchanged",
                  "Is the frozen acceptance byte-identical?",
                  not drift,
                  "; ".join(f"{d['path']}: {d['problem']}" for d in drift)
                  or f"{len(frozen_digests)} frozen file(s) unchanged")
        else:
            check("frozen_acceptance_unchanged",
                  "Is the frozen acceptance byte-identical?",
                  True, "no frozen digests were declared for this package")

        # 10. Required artefacts exist and are not empty.
        missing = []
        for artefact in (required_artefacts or []):
            path = Path(artefact)
            if not path.is_file():
                missing.append(f"{artefact}: absent")
            elif path.stat().st_size == 0:
                missing.append(f"{artefact}: empty")
        check("required_artefacts_present",
              "Does every mandatory artefact exist and contain something?",
              not missing,
              "; ".join(missing) or
              f"{len(required_artefacts or [])} artefact(s) present and non-empty")

        # 11. No open Critical or High finding.
        blocking = self.store.open_blocking_findings(wp_id)
        check("no_blocking_findings",
              "Is anything Critical or High still open?",
              not blocking,
              "; ".join(f["summary"] for f in blocking)
              or "no open Critical/High finding")

        verdict = "MERGE_ELIGIBLE" if all(c.passed for c in checks) else "REFUSED"
        return GateResult(
            wp_id=wp_id, candidate_revision=candidate_revision, verdict=verdict,
            checks=checks,
            evaluated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
