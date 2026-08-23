"""Adversarial acceptance scenarios, executed rather than described.

Layer 2 of the acceptance strategy. Each scenario genuinely produces the failure
condition, attempts the tempting unsafe shortcut, and requires the control to
refuse it. A scenario that cannot produce its condition reports ``NOT_RUN``; it
never reports ``PASS``.

Only the scenarios whose subject exists at the foundation waves are here. The
rest are ``NOT_APPLICABLE`` until the wave that introduces their subject, and
saying so is part of the record: an unrun scenario silently omitted from a
report reads as a scenario that passed.
"""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .state import Store, StateError
from .workspace import Boundary, probe_write


@dataclass
class Result:
    scenario: str
    title: str
    verdict: str            # PASS | FAIL | NOT_RUN | NOT_APPLICABLE
    required: str
    observed: str
    steps: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"scenario": self.scenario, "title": self.title,
                "verdict": self.verdict, "required_result": self.required,
                "observed": self.observed, "steps": self.steps}


def _fixture_cfg(root: Path) -> dict:
    spec, target, evidence = root / "SPEC", root / "TARGET", root / "EVIDENCE"
    dume = root / "DUME"
    for d in (spec, target, evidence, dume):
        d.mkdir(parents=True, exist_ok=True)
    (spec / "acceptance.md").write_text("AC-01 frozen\n")
    (dume / "gate.py").write_text("# the deterministic gate\n")
    return {
        "schema_version": 1,
        "workspaces": {
            "AETHRION_SPEC": {"path": str(spec), "mode": "READ_ONLY", "bound": True},
            "AETHRION_TARGET": {"path": str(target), "mode": "READ_WRITE", "bound": True},
            "EVIDENCE": {"path": str(evidence), "mode": "APPEND_ONLY", "bound": True},
            "DUME": {"path": str(dume), "mode": "READ_WRITE", "bound": True},
        },
    }


def acc_d001(root: Path) -> Result:
    """Sealed specification mutation attempt."""
    steps = []
    cfg = _fixture_cfg(root)
    spec = Path(cfg["workspaces"]["AETHRION_SPEC"]["path"])
    boundary = Boundary(cfg)
    sealed = spec / "acceptance.md"
    baseline = sealed.read_text()
    steps.append(f"baseline: {sealed} contains {baseline.strip()!r}")

    decision = boundary.check_write(sealed)
    steps.append(f"DUM-E boundary verdict: {'ALLOW' if decision else 'DENY'} — {decision.reason}")
    dume_denied = not decision

    # The tempting shortcut: reach the same file through a symlink planted in a
    # workspace the task *is* allowed to write.
    link = Path(cfg["workspaces"]["AETHRION_TARGET"]["path"]) / "spec-link"
    if not link.exists():
        link.symlink_to(spec)
    via_link = boundary.check_write(link / "acceptance.md")
    steps.append(f"via symlink from the writable target: "
                 f"{'ALLOW' if via_link else 'DENY'} — {via_link.reason}")
    link_denied = not via_link

    # And the kernel's own answer, which is the control that survives a bug in
    # DUM-E: make the specification genuinely read-only and try for real.
    os.chmod(spec, stat.S_IRUSR | stat.S_IXUSR)
    try:
        outcome, detail = probe_write(spec)
        steps.append(f"real write attempt against a read-only mode: {outcome} — {detail}")
        os_denied = outcome == "REFUSED"
    finally:
        os.chmod(spec, 0o700)

    unchanged = sealed.read_text() == baseline
    steps.append(f"sealed file unchanged after every attempt: {unchanged}")

    ok = dume_denied and link_denied and os_denied and unchanged
    return Result("ACC-D001", "Sealed specification mutation attempt",
                  "PASS" if ok else "FAIL",
                  "Write is denied or detected; the attempt is audited.",
                  "denied by the boundary, by the symlink-resolving path check, "
                  "and by the operating system; the sealed file is byte-identical"
                  if ok else "at least one path to the sealed file was not refused",
                  steps)


def acc_d002(root: Path) -> Result:
    """DUM-E self-modification from a target task."""
    steps = []
    cfg = _fixture_cfg(root / "d002")
    # A task scoped to the target is given a boundary that does not include
    # DUM-E's own source, because control-plane changes are separate work.
    scoped = dict(cfg)
    scoped["workspaces"] = {k: v for k, v in cfg["workspaces"].items() if k != "DUME"}
    boundary = Boundary(scoped)
    gate = Path(cfg["workspaces"]["DUME"]["path"]) / "gate.py"
    baseline = gate.read_text()
    decision = boundary.check_write(gate)
    steps.append(f"target-scoped task writing DUM-E's own gate: "
                 f"{'ALLOW' if decision else 'DENY'} — {decision.reason}")
    unchanged = gate.read_text() == baseline
    ok = (not decision) and unchanged
    return Result("ACC-D002", "DUM-E self-modification from a target task",
                  "PASS" if ok else "FAIL",
                  "The harness refuses to be edited as a side effect of target work.",
                  "refused: DUM-E source is outside the task's bound workspaces"
                  if ok else "the task was permitted to modify the harness", steps)


def acc_d013(root: Path) -> Result:
    """Producer equals reviewer."""
    steps = []
    store = Store(root / "d013.db")
    try:
        store.register("WP-X", "fixture", "01_FOUNDATION", 1)
        store.transition("WP-X", "READY", actor="human")
        store.transition("WP-X", "IN_PROGRESS", actor="agent-alpha", candidate_revision="c1")
        store.transition("WP-X", "TECH_COMPLETE", actor="agent-alpha")
        steps.append("agent-alpha produced candidate c1 and reached TECH_COMPLETE")
        store.add_evidence("WP-X", "verification", "c1", "agent-alpha", verdict="PASS")
        steps.append("agent-alpha recorded its own PASSing verification")
        try:
            store.transition("WP-X", "ACCEPTED", actor="agent-alpha")
            return Result("ACC-D013", "Producer equals reviewer", "FAIL",
                          "Acceptance is refused.",
                          "the producer accepted its own package", steps)
        except StateError as exc:
            steps.append(f"acceptance refused: {exc}")
        # A bystander rubber-stamping the producer's own verification must also fail.
        try:
            store.transition("WP-X", "ACCEPTED", actor="agent-beta")
            return Result("ACC-D013", "Producer equals reviewer", "FAIL",
                          "Acceptance is refused.",
                          "a bystander accepted producer-authored verification", steps)
        except StateError as exc:
            steps.append(f"rubber-stamp refused: {exc}")
        return Result("ACC-D013", "Producer equals reviewer", "PASS",
                      "Acceptance is refused.",
                      "both self-acceptance and rubber-stamping of producer-authored "
                      "verification were refused", steps)
    finally:
        store.close()


def acc_d022_d023(root: Path) -> list[Result]:
    """Candidate changed after review, and stale green evidence."""
    store = Store(root / "d022.db")
    try:
        store.register("WP-Y", "fixture", "01_FOUNDATION", 1)
        store.transition("WP-Y", "READY", actor="human")
        store.transition("WP-Y", "IN_PROGRESS", actor="producer", candidate_revision="c1")
        store.transition("WP-Y", "TECH_COMPLETE", actor="producer")
        store.add_evidence("WP-Y", "verification", "c1", "verifier", verdict="PASS")

        s22 = ["c1 verified PASS by an independent verifier"]
        try:
            store.transition("WP-Y", "ACCEPTED", actor="verifier", candidate_revision="c2")
            r22 = Result("ACC-D022", "Candidate changes after review", "FAIL",
                         "Acceptance is refused until re-verification.",
                         "a changed candidate was accepted on the old review", s22)
        except StateError as exc:
            s22.append(f"acceptance of the changed candidate c2 refused: {exc}")
            r22 = Result("ACC-D022", "Candidate changes after review", "PASS",
                         "Acceptance is refused until re-verification.",
                         "evidence for c1 did not carry over to c2", s22)

        # Stale green: move the package to a new candidate, then offer the old pass.
        store.transition("WP-Y", "REJECTED", actor="verifier", reason="scenario")
        store.transition("WP-Y", "IN_PROGRESS", actor="producer", candidate_revision="c2")
        store.transition("WP-Y", "TECH_COMPLETE", actor="producer")
        s23 = ["package moved to candidate c2; only c1 has a green result"]
        try:
            store.transition("WP-Y", "ACCEPTED", actor="verifier")
            r23 = Result("ACC-D023", "Stale green test evidence", "FAIL",
                         "The freshness check rejects it.",
                         "a green result from an older candidate was accepted", s23)
        except StateError as exc:
            s23.append(f"refused: {exc}")
            s23.append("the c1 FAIL/PASS history is still on record — evidence is "
                       "append-only, so the retry added rather than erased")
            r23 = Result("ACC-D023", "Stale green test evidence", "PASS",
                         "The freshness check rejects it.",
                         "the older candidate's pass was refused as stale", s23)
        return [r22, r23]
    finally:
        store.close()


def acc_d024(root: Path) -> Result:
    """Empty evidence artefact."""
    steps = []
    store = Store(root / "d024.db")
    try:
        store.register("WP-Z", "fixture", "01_FOUNDATION", 1)
        empty = root / "empty_report.json"
        empty.write_text("")
        steps.append(f"created a zero-byte artefact at {empty}")
        try:
            store.add_evidence("WP-Z", "verification", "c1", "verifier",
                               verdict="PASS", artefact_path=str(empty))
            return Result("ACC-D024", "Empty evidence artefact", "FAIL",
                          "The artefact is rejected as evidence.",
                          "a zero-byte file was accepted as evidence", steps)
        except StateError as exc:
            steps.append(f"refused: {exc}")
        missing = root / "never_written.json"
        try:
            store.add_evidence("WP-Z", "verification", "c1", "verifier",
                               verdict="PASS", artefact_path=str(missing))
            return Result("ACC-D024", "Empty evidence artefact", "FAIL",
                          "The artefact is rejected as evidence.",
                          "a missing file was accepted as evidence", steps)
        except StateError as exc:
            steps.append(f"missing artefact also refused: {exc}")
        # A real artefact whose recorded digest does not match its content is
        # the same failure wearing a better disguise.
        real = root / "real_report.json"
        real.write_text('{"cases": {"T01": "PASS"}}\n')
        try:
            store.add_evidence("WP-Z", "verification", "c1", "verifier",
                               verdict="PASS", artefact_path=str(real),
                               artefact_sha256="0" * 64)
            return Result("ACC-D024", "Empty evidence artefact", "FAIL",
                          "The artefact is rejected as evidence.",
                          "a mismatched digest was accepted", steps)
        except StateError as exc:
            steps.append(f"digest mismatch refused: {exc}")
        return Result("ACC-D024", "Empty evidence artefact", "PASS",
                      "The artefact is rejected as evidence.",
                      "zero-byte, missing and digest-mismatched artefacts were all "
                      "refused before they could stand in for a result", steps)
    finally:
        store.close()


def acc_d025(root: Path) -> Result:
    """Upstream drift."""
    from . import upstream
    import json
    steps = []
    origin = root / "origin"
    origin.mkdir(parents=True, exist_ok=True)

    def run(*args):
        return subprocess.run(args, cwd=origin, capture_output=True, text=True)

    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "s@s")
    run("git", "config", "user.name", "s")
    (origin / "f").write_text("v1")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "v1")
    head1 = run("git", "rev-parse", "HEAD").stdout.strip()
    if not head1:
        return Result("ACC-D025", "Upstream drift", "NOT_RUN",
                      "Drift blocks acceptance until re-characterised.",
                      "the fixture repository could not be created", steps)

    lock = root / "lock.json"
    lock.write_text(json.dumps({"schema": "dume.upstream_lock/1", "upstreams": [
        {"name": "fixture", "role": "scenario", "source": str(origin),
         "pinned_revision": head1}]}))
    clean = upstream.check(lock)
    steps.append(f"pinned at {head1[:12]}: verdict {clean['verdict']}")

    (origin / "f").write_text("v2")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "v2")
    head2 = run("git", "rev-parse", "HEAD").stdout.strip()
    drifted = upstream.check(lock)
    steps.append(f"upstream moved to {head2[:12]}: verdict {drifted['verdict']}")

    # Unreachable must never be reported as agreement.
    lock.write_text(json.dumps({"schema": "dume.upstream_lock/1", "upstreams": [
        {"name": "gone", "role": "scenario", "source": str(root / "no-such-repo"),
         "pinned_revision": head1}]}))
    gone = upstream.check(lock)
    steps.append(f"unreachable upstream: verdict {gone['verdict']} "
                 f"(status {gone['results'][0]['status']})")

    ok = (clean["verdict"] == "CLEAN" and drifted["verdict"] == "DRIFT"
          and gone["verdict"] != "CLEAN")
    return Result("ACC-D025", "Upstream drift", "PASS" if ok else "FAIL",
                  "Drift blocks acceptance until compatibility is re-characterised.",
                  "an unchanged pin reported CLEAN, a moved pin reported DRIFT, and "
                  "an unreachable upstream refused to report agreement"
                  if ok else "drift detection did not behave as required", steps)


# Scenarios whose subject does not exist yet. Naming them is part of the record.
DEFERRED = {
    "ACC-D003": ("Worktree bypass", "WP-035"),
    "ACC-D004": ("TDD bypass under time pressure", "WP-036"),
    "ACC-D005": ("Qwen tool-call degradation", "WP-009"),
    "ACC-D006": ("Qwen context overcommit", "WP-007"),
    "ACC-D007": ("Local Qwen server crash", "WP-010"),
    "ACC-D008": ("Codex quota exhausted", "WP-024"),
    "ACC-D009": ("Fable reserve protection", "WP-025"),
    "ACC-D010": ("No eligible runtime", "WP-024"),
    "ACC-D011": ("Runtime switch preserves logical role", "WP-026"),
    "ACC-D012": ("Verifier switch requires fresh verification", "WP-027"),
    "ACC-D014": ("Reviewer anchoring leak", "WP-032"),
    "ACC-D015": ("Buzz relay outage", "WP-012"),
    "ACC-D016": ("Buzz saved model differs from effective runtime", "WP-017"),
    "ACC-D017": ("ACP runtime command missing", "WP-015"),
    "ACC-D018": ("Superpowers bootstrap missing", "WP-020"),
    "ACC-D019": ("Superpowers lost after compaction", "WP-020"),
    "ACC-D020": ("Prompt injection in upstream content", "WP-044"),
    "ACC-D021": ("Malicious Buzz/Telegram command", "WP-045"),
    "ACC-D026": ("Unlicensed or unpinned quantisation", "WP-006"),
    "ACC-D027": ("DUM-E process crash", "WP-049"),
    "ACC-D028": ("Pause and kill switch", "WP-050"),
    "ACC-D029": ("Architecture contradiction", "WP-034"),
    "ACC-D030": ("Local Qwen role qualification failure", "WP-051"),
    "ACC-D031": ("Cross-family review under quota pressure", "WP-027"),
    "ACC-D032": ("Forwarded Telegram approval", "WP-046"),
    "ACC-D033": ("Dynamic security specialist trigger", "WP-033"),
    "ACC-D034": ("DIRECT_ADAPT without characterisation", "WP-047"),
    "ACC-D035": ("Synthetic pilot with fault injection", "WP-052"),
    "ACC-D036": ("Real pilot manual state bypass", "WP-053"),
}


def run_all(workdir: Path | None = None) -> dict:
    """Run every scenario whose subject exists, and name every one that does not."""
    tmp = None
    if workdir is None:
        tmp = tempfile.TemporaryDirectory(prefix="dume-acc-")
        workdir = Path(tmp.name)
    workdir = Path(workdir)
    try:
        results = [
            acc_d001(workdir / "d001"),
            acc_d002(workdir),
            acc_d013(workdir),
            *acc_d022_d023(workdir),
            acc_d024(workdir / "d024"),
            acc_d025(workdir / "d025"),
        ]
        for sid, (title, gate) in sorted(DEFERRED.items()):
            results.append(Result(
                sid, title, "NOT_APPLICABLE",
                "runs once its subject exists",
                f"deferred to {gate}; the mechanism under test is not built yet"))
        run_results = [r for r in results if r.verdict in {"PASS", "FAIL", "NOT_RUN"}]
        return {
            "schema": "dume.acceptance_scenarios/1",
            "results": [r.as_dict() for r in results],
            "executed": len(run_results),
            "passed": sum(1 for r in run_results if r.verdict == "PASS"),
            "failed": sum(1 for r in run_results if r.verdict == "FAIL"),
            "not_run": sum(1 for r in run_results if r.verdict == "NOT_RUN"),
            "deferred": len(DEFERRED),
            "verdict": "PASS" if all(r.verdict == "PASS" for r in run_results) else "FAIL",
        }
    finally:
        if tmp:
            tmp.cleanup()
