"""Real agents. The models actually do the work.

`SyntheticExecutor` proves the pipeline; this proves the pipeline is worth
having. Every role here is a live model call against a bound runtime, and every
verdict is recorded against the agent identity that produced it.

Three properties the harness must hold, and holds here:

* **Context projection.** Each role is handed the frozen packet, its own role
  card, and the artefact its question is about — never another agent's
  conversation. The reviewer does not see the implementer's reasoning, and the
  verifier does not see either reviewer's verdict, because a second opinion
  that has read the first is not a second opinion.
* **RED before GREEN is observed, not claimed.** The implementer's loop refuses
  to proceed until `run_tests` has actually returned non-zero, and refuses to
  finish until it returns zero. Both exit codes are recorded.
* **A model's PASS is not evidence of behaviour.** For verification the exit
  code from a fresh checkout is the evidence; the model interprets it and may
  not overrule it.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..packets.wp_packet_builder import WPPacket
from ..runtimes.client import ModelClient, ModelError
from .agent_tools import TOOL_SCHEMAS, ToolLog, Toolbox

MAX_TOOL_TURNS = 24


class ImplementationRefused(RuntimeError):
    """The implementer could not produce a red-then-green candidate."""


def _packet_brief(packet: WPPacket, limit: int = 6000) -> str:
    """The frozen packet, projected for a model rather than summarised by one.

    Truncation is by section with the boundary marked, so nothing silently
    disappears — a reader can see that a section was cut and how much.
    """
    parts = [f"# {packet.wp_id} — {packet.title}",
             f"workstream: {packet.workstream}   wave: {packet.wave}",
             f"packet digest: {packet.packet_sha256[:16]}", ""]
    for section in packet.sections:
        body = section.text
        if len(body) > limit:
            body = body[:limit] + f"\n[…{len(section.text) - limit} more characters]"
        parts += [f"## {section.name} ({section.path})", body, ""]
    if packet.deliverables:
        parts += ["## mandatory deliverables"] + \
                 [f"- {d}" for d in packet.deliverables] + [""]
    if packet.known_failure_modes:
        parts += ["## known failure modes that must be controlled"] + \
                 [f"- {f}" for f in packet.known_failure_modes] + [""]
    parts += ["## actions that are forbidden, without exception"] + \
             [f"- {f}" for f in packet.forbidden]
    return "\n".join(parts)


ROLE_CARDS = {
    "architect": (
        "You are the Architect. You turn a frozen work-package packet into an "
        "implementation plan. You decide the shape of the change and whether "
        "the requirement is satisfiable as written. You do not write the "
        "implementation and you do not judge it afterwards. If the requirement "
        "cannot be satisfied without violating something the packet forbids, "
        "say so — that is an escalation, not a failure."),
    "implementer": (
        "You are the Implementer. You produce the candidate under strict "
        "test-first discipline.\n\n"
        "The order is not negotiable:\n"
        "1. Write a test that captures the required behaviour.\n"
        "2. Call run_tests. It MUST fail. A test that passes before the "
        "implementation exists is testing nothing, and you must fix the test "
        "rather than continue.\n"
        "3. Write the smallest implementation that makes it pass.\n"
        "4. Call run_tests again. It MUST pass.\n\n"
        "You have no authority over whether your own work is correct. Do not "
        "modify any frozen acceptance criteria or specification — those files "
        "are protected and an attempt is recorded as a Critical finding."),
    "spec_reviewer": (
        "You are the Specification Compliance Reviewer. You answer exactly one "
        "question: was the requirement met? You are not asked whether the code "
        "is good, whether it is fast, or whether you would have written it "
        "differently. Judge the candidate against the frozen specification you "
        "are given and nothing else. You have not seen the implementer's "
        "reasoning and you should not ask for it."),
    "code_reviewer": (
        "You are the Code Quality Reviewer. You answer exactly one question: is "
        "the implementation good? Structure, clarity, error handling, whether "
        "the tests actually test something. You are not asked whether the "
        "requirement was met — a different reviewer answered that, and you have "
        "deliberately not been shown their verdict."),
    "verifier": (
        "You are the Independent Verifier. You answer exactly one question: "
        "does it actually work? You are given the output of the acceptance "
        "suite run from a fresh checkout, in a directory the implementer never "
        "touched. The exit code is the evidence. You interpret it; you cannot "
        "overrule it. If the suite failed, the verdict is FAIL however "
        "reasonable the code looks."),
}

VERDICT_SCHEMA = ('{"verdict": "PASS" or "FAIL", "reason": "<one or two '
                  'sentences>", "findings": [{"severity": "CRITICAL|HIGH|'
                  'MEDIUM|LOW", "summary": "<what is wrong>"}]}')


@dataclass
class ModelExecutor:
    """Drives one work package with real models behind each role."""

    worktrees: object
    clients: dict            # role_id -> ModelClient
    evidence_dir: Path
    bindings: dict = field(default_factory=dict)   # role_id -> RuntimeBinding
    transcripts: dict = field(default_factory=dict)

    def _client(self, role: str) -> ModelClient:
        try:
            return self.clients[role]
        except KeyError:
            raise ModelError(f"no runtime bound for role {role!r}") from None

    def _record(self, role: str, messages: list, reply_text: str) -> None:
        """Keep each role's conversation separate and on disk.

        Separate because context projection is a property of the system, not a
        promise: if every role shared one list, the embargo would be a comment.
        """
        self.transcripts.setdefault(role, []).append(
            {"messages": messages[-2:], "reply": reply_text[:4000]})

    # ---- roles ----------------------------------------------------------

    def plan(self, packet: WPPacket, cohort) -> dict:
        client = self._client("architect")
        messages = [
            {"role": "system", "content": ROLE_CARDS["architect"]},
            {"role": "user", "content":
             _packet_brief(packet) + "\n\n"
             f"Assurance level: {cohort.assurance_level}.\n\n"
             "Produce an implementation plan as JSON with keys: summary (one "
             "sentence), satisfiable (true/false), steps (array of strings), "
             "test_first (a string describing the failing test to write first), "
             "risks (array of strings)."}]
        try:
            plan = client.json_reply(
                messages,
                '{"summary": "...", "satisfiable": true, "steps": [...], '
                '"test_first": "...", "risks": [...]}')
        except ModelError as exc:
            return {"summary": f"planning failed: {exc}", "satisfiable": False,
                    "steps": [], "risks": [str(exc)]}
        self._record("architect", messages, json.dumps(plan)[:2000])
        plan.setdefault("summary", "plan produced")
        return plan

    def prepare_worktree(self, packet: WPPacket):
        task_id = f"live-{packet.packet_sha256[:8]}"
        return self.worktrees.create(task_id, packet.wp_id)

    def implement(self, packet: WPPacket, plan: dict, worktree) -> dict:
        """Drive the implementer until a red run and then a green run exist."""
        client = self._client("implementer")
        log = ToolLog()
        tools = Toolbox(worktree.path, log)
        root = Path(worktree.path)

        messages = [
            {"role": "system", "content": ROLE_CARDS["implementer"]},
            {"role": "user", "content":
             _packet_brief(packet, limit=3500) + "\n\n"
             "## the accepted plan\n" + json.dumps(plan, indent=2)[:2000] + "\n\n"
             "Work only through the tools. Start by writing the failing test. "
             "When run_tests has failed once and then passed once, reply with "
             "the single word DONE and nothing else."}]

        red_exit: int | None = None
        green_exit: int | None = None
        for turn in range(MAX_TOOL_TURNS):
            reply = client.chat(messages, tools=TOOL_SCHEMAS, max_tokens=3000)
            if not reply.tool_calls:
                text = (reply.content or "").strip()
                if "DONE" in text.upper() and green_exit == 0:
                    break
                messages.append({"role": "assistant", "content": text[:2000]})
                messages.append({"role": "user", "content":
                    "Continue using the tools. "
                    + ("You have not yet observed a failing test run."
                       if red_exit is None else
                       "You have a failing run; now make it pass."
                       if green_exit != 0 else
                       "Reply DONE.")})
                continue

            messages.append({"role": "assistant", "content": reply.content or "",
                             "tool_calls": [
                                 {"id": c.id, "type": "function",
                                  "function": {"name": c.name,
                                               "arguments": c.raw_arguments}}
                                 for c in reply.tool_calls]})
            for call in reply.tool_calls:
                if call.parse_error:
                    result = {"ok": False,
                              "error": f"arguments were not JSON: {call.parse_error}"}
                else:
                    result = tools.dispatch(call.name, call.arguments)
                if call.name == "run_tests" and result.get("ok"):
                    if red_exit is None and result["exit_code"] != 0:
                        red_exit = result["exit_code"]
                    elif red_exit is not None and result["exit_code"] == 0:
                        green_exit = 0
                    elif red_exit is None and result["exit_code"] == 0:
                        result["harness_note"] = (
                            "The suite passed before any implementation exists. "
                            "That test is not testing the required behaviour. "
                            "Fix the test.")
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": json.dumps(result)[:3000]})
            if green_exit == 0 and red_exit is not None:
                break

        self._record("implementer", messages, f"red={red_exit} green={green_exit}")
        discipline_dir = self.evidence_dir / packet.wp_id
        discipline_dir.mkdir(parents=True, exist_ok=True)
        (discipline_dir / "tool_log.json").write_text(
            json.dumps({"calls": log.calls}, indent=2))

        if red_exit is None or green_exit != 0:
            raise ImplementationRefused(
                f"no red-then-green cycle was observed "
                f"(red={red_exit}, green={green_exit}) after "
                f"{len(log.calls)} tool call(s)")

        subprocess.run(["git", "-C", str(root), "add", "-A"],
                       capture_output=True, check=False)
        subprocess.run(["git", "-C", str(root), "commit", "-qm",
                        f"{packet.wp_id}: candidate"], capture_output=True, check=False)
        candidate = self.worktrees.candidate_revision(worktree)

        red_log = discipline_dir / "red.txt"
        green_log = discipline_dir / "green.txt"
        runs = log.test_runs()
        red_log.write_text(f"exit={red_exit}\n" + json.dumps(runs[:1], indent=2))
        green_log.write_text(f"exit={green_exit}\n" + json.dumps(runs[-1:], indent=2))

        return {"candidate_revision": candidate,
                "discipline": f"RED exit={red_exit}, GREEN exit={green_exit}, "
                              f"{len(log.calls)} tool calls",
                "required_artefacts": [str(red_log), str(green_log),
                                       str(discipline_dir / "tool_log.json")],
                "frozen_digests": {}, "tool_calls": len(log.calls)}

    def review(self, kind: str, packet: WPPacket, worktree, candidate: str) -> dict:
        if kind == "verification":
            return self._verify(packet, worktree, candidate)
        role = {"specification_compliance": "spec_reviewer",
                "code_quality": "code_reviewer"}[kind]
        client = self._client(role)
        diff = subprocess.run(
            ["git", "-C", worktree.path, "diff",
             f"{worktree.base_revision}..{candidate}"],
            capture_output=True, text=True).stdout[:12000]

        if role == "spec_reviewer":
            question = ("Was the requirement met? Judge the diff against the "
                        "frozen specification and acceptance criteria above.")
            context = _packet_brief(packet, limit=5000)
        else:
            question = ("Is the implementation good? Judge structure, clarity, "
                        "error handling, and whether the tests test something.")
            # The code reviewer gets the deliverables, not the whole
            # specification: it is not being asked whether the requirement was
            # met, and handing it that question's material invites it to answer
            # the other reviewer's question instead of its own.
            context = (f"# {packet.wp_id} — {packet.title}\n\n"
                       "## mandatory deliverables\n"
                       + "\n".join(f"- {d}" for d in packet.deliverables))

        messages = [
            {"role": "system", "content": ROLE_CARDS[role]},
            {"role": "user", "content":
             f"{context}\n\n## the candidate diff ({candidate[:12]})\n"
             f"```diff\n{diff}\n```\n\n{question}\n\n"
             f"Reply with only a JSON object: {VERDICT_SCHEMA}"}]
        try:
            result = client.json_reply(messages, VERDICT_SCHEMA)
        except ModelError as exc:
            return {"verdict": "FAIL", "failure_class": "RUNTIME_FAILURE",
                    "detail": f"the reviewer runtime failed: {exc}"}
        self._record(role, messages, json.dumps(result)[:2000])

        verdict = str(result.get("verdict", "")).upper()
        if verdict not in {"PASS", "FAIL"}:
            return {"verdict": "FAIL", "failure_class": "HARNESS_FAILURE",
                    "detail": f"reviewer returned an unusable verdict: {result}"}
        artefact = self.evidence_dir / packet.wp_id / f"{kind}.json"
        artefact.parent.mkdir(parents=True, exist_ok=True)
        artefact.write_text(json.dumps(result, indent=2))
        return {"verdict": verdict, "detail": str(result.get("reason", ""))[:300],
                "artefact": str(artefact),
                "findings": result.get("findings") or []}

    def _verify(self, packet: WPPacket, worktree, candidate: str) -> dict:
        """Fresh checkout, fresh run. The exit code is the evidence."""
        fresh = self.evidence_dir / packet.wp_id / "fresh"
        if fresh.exists():
            shutil.rmtree(fresh)
        fresh.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--no-local", worktree.path, str(fresh)],
                       capture_output=True, check=False)
        subprocess.run(["git", "-C", str(fresh), "checkout", "-q", candidate],
                       capture_output=True, check=False)
        run = subprocess.run([sys.executable, "-m", "pytest", "-q", str(fresh)],
                             cwd=str(fresh), capture_output=True, text=True)
        output = (run.stdout + run.stderr)[-4000:]
        log = self.evidence_dir / packet.wp_id / "fresh_verification.txt"
        log.write_text(f"candidate={candidate}\nexit={run.returncode}\n{output}")

        client = self._client("verifier")
        messages = [
            {"role": "system", "content": ROLE_CARDS["verifier"]},
            {"role": "user", "content":
             f"Candidate {candidate[:12]} was checked out fresh and the suite "
             f"was run.\n\nexit code: {run.returncode}\n\noutput:\n"
             f"```\n{output[-3000:]}\n```\n\n"
             f"Does it actually work? Reply with only: {VERDICT_SCHEMA}"}]
        try:
            result = client.json_reply(messages, VERDICT_SCHEMA)
            claimed = str(result.get("verdict", "")).upper()
            reason = str(result.get("reason", ""))[:300]
        except ModelError as exc:
            claimed, reason, result = "", f"verifier runtime failed: {exc}", {}

        # The model interprets the exit code. It does not get to overrule it.
        actual = "PASS" if run.returncode == 0 else "FAIL"
        overruled = claimed and claimed != actual
        self._record("verifier", messages, json.dumps(result)[:2000])
        return {"verdict": actual,
                "failure_class": "IMPLEMENTATION_FAILURE",
                "artefact": str(log),
                "detail": (f"fresh checkout, pytest exit={run.returncode}; "
                           f"verifier said {claimed or '—'}"
                           + (" and was overruled by the exit code"
                              if overruled else "")
                           + f" — {reason}"),
                "findings": (result.get("findings") or []) if actual == "FAIL" else []}
