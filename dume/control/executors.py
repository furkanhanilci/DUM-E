"""What actually performs the work each role represents.

The orchestrator guarantees sequence, independence and evidence. It does not
guarantee intelligence, and it does not need to know where the intelligence
comes from. An executor is the seam between the two.

``SyntheticExecutor`` below is deliberately not an LLM. It exists so the harness
can be proven end to end before a single model is bound: it really creates a
worktree, really writes code, really runs the tests, and really returns the exit
codes it got. What is simulated is judgement, not evidence — every PASS it
returns is a command that exited zero.

That distinction is the point. A pilot that stubs the *tests* proves nothing; a
pilot that stubs the *reasoning* proves the pipeline.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..packets.wp_packet_builder import WPPacket
from ..state import sha256_file


class SyntheticExecutor:
    """A deterministic stand-in for a cohort, used to characterise the harness.

    Fault injection is explicit: ``inject`` names a step that must go wrong, so
    a pilot can prove the failure paths as well as the happy one.
    """

    def __init__(self, worktree_manager, *, evidence_dir: Path,
                 inject: str | None = None):
        self.worktrees = worktree_manager
        self.evidence_dir = Path(evidence_dir)
        self.inject = inject
        self._worktree = None

    # ---- roles ----------------------------------------------------------

    def plan(self, packet: WPPacket, cohort) -> dict:
        return {
            "summary": (f"{len(packet.deliverables)} deliverable(s), "
                        f"{cohort.assurance_level} assurance, "
                        f"{len(cohort.roles)} roles"),
            "deliverables": packet.deliverables,
        }

    def prepare_worktree(self, packet: WPPacket):
        task_id = f"synthetic-{packet.packet_sha256[:8]}"
        self._worktree = self.worktrees.create(task_id, packet.wp_id)
        return self._worktree

    def implement(self, packet: WPPacket, plan: dict, worktree,
                  findings: list[dict] | None = None) -> dict:
        """RED then GREEN, with the exit codes to prove which was which."""
        root = Path(worktree.path)
        module = root / "target_package.py"
        test = root / "test_target_package.py"

        # RED — the test exists and fails, because the behaviour does not.
        test.write_text(
            '"""The behaviour this package is required to provide."""\n'
            "import target_package\n\n\n"
            "def test_capacity_envelope_refuses_to_flatter_the_host():\n"
            "    # An envelope that reports more usable memory than exists is\n"
            "    # worse than no envelope, because it is trusted.\n"
            "    assert target_package.usable_bytes(48, overhead=0.12) < 48\n\n\n"
            "def test_zero_capacity_is_not_negative():\n"
            "    assert target_package.usable_bytes(0) == 0\n")
        module.write_text("# not implemented yet\n")
        red = self._pytest(root)
        if red.returncode == 0:
            raise RuntimeError(
                "the RED step passed, so the test does not test anything")

        # GREEN — the smallest change that makes it pass.
        if self.inject == "implementation":
            module.write_text(
                "def usable_bytes(total, overhead=0.12):\n"
                "    return total  # deliberately wrong: ignores the overhead\n")
        else:
            module.write_text(
                '"""The bounded capability this package owes."""\n\n\n'
                "def usable_bytes(total, overhead=0.12):\n"
                "    \"\"\"Total minus the runtime reserve, never below zero.\"\"\"\n"
                "    if total <= 0:\n"
                "        return 0\n"
                "    return total * (1 - overhead)\n")
        green = self._pytest(root)

        if self.inject == "protected_path":
            # The tempting shortcut: edit the frozen acceptance so the result
            # matches the criterion instead of the other way round.
            frozen = root / "acceptance" / "frozen.md"
            frozen.parent.mkdir(exist_ok=True)
            frozen.write_text("AC-01 relaxed to match the implementation\n")

        subprocess.run(["git", "-C", str(root), "add", "-A"],
                       capture_output=True, check=False)
        subprocess.run(["git", "-C", str(root), "commit", "-qm",
                        f"{packet.wp_id}: synthetic candidate"],
                       capture_output=True, check=False)
        candidate = self.worktrees.candidate_revision(worktree)

        red_log = self.evidence_dir / packet.wp_id / "red.txt"
        green_log = self.evidence_dir / packet.wp_id / "green.txt"
        for path, result in ((red_log, red), (green_log, green)):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"exit={result.returncode}\n{result.stdout}\n{result.stderr}")

        return {
            "candidate_revision": candidate,
            "discipline": f"RED exit={red.returncode}, GREEN exit={green.returncode}",
            "required_artefacts": [str(red_log), str(green_log)],
            "frozen_digests": {},
        }

    def review(self, kind: str, packet: WPPacket, worktree, candidate: str) -> dict:
        root = Path(worktree.path)
        if kind == "specification_compliance":
            if self.inject == "spec_review":
                return {"verdict": "FAIL", "failure_class": "SPEC_MISINTERPRETATION",
                        "detail": "the candidate implements a different requirement",
                        "findings": [{"severity": "HIGH",
                                      "summary": "deliverable not satisfied"}]}
            present = (root / "target_package.py").is_file()
            return {"verdict": "PASS" if present else "FAIL",
                    "detail": f"{len(packet.deliverables)} deliverable(s) checked "
                              f"against the frozen card"}
        if kind == "code_quality":
            # A real, mechanical quality signal rather than an opinion: does the
            # module compile, and does it carry a docstring explaining itself?
            source = (root / "target_package.py").read_text()
            compiles = True
            try:
                compile(source, "target_package.py", "exec")
            except SyntaxError:
                compiles = False
            documented = '"""' in source
            ok = compiles and documented
            return {"verdict": "PASS" if ok else "FAIL",
                    "detail": f"compiles={compiles}, documented={documented}"}
        if kind == "verification":
            # Fresh verification: a clean checkout of the candidate in a
            # directory the implementer never touched, and the suite run there.
            fresh = self.evidence_dir / packet.wp_id / "fresh"
            if fresh.exists():
                import shutil
                shutil.rmtree(fresh)
            fresh.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "-q", "--no-local", str(root), str(fresh)],
                           capture_output=True, check=False)
            subprocess.run(["git", "-C", str(fresh), "checkout", "-q", candidate],
                           capture_output=True, check=False)
            result = self._pytest(fresh)
            log = self.evidence_dir / packet.wp_id / "fresh_verification.txt"
            log.write_text(f"exit={result.returncode}\n{result.stdout}\n{result.stderr}")
            return {"verdict": "PASS" if result.returncode == 0 else "FAIL",
                    "failure_class": "IMPLEMENTATION_FAILURE",
                    "artefact": str(log),
                    "detail": f"fresh checkout of {candidate[:12]}, "
                              f"pytest exit={result.returncode}"}
        raise ValueError(f"unknown review kind: {kind}")

    # ---- helpers --------------------------------------------------------

    def _pytest(self, cwd: Path) -> subprocess.CompletedProcess:
        import sys
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", str(cwd)],
            cwd=str(cwd), capture_output=True, text=True)
