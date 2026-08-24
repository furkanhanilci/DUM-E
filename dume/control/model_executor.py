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
from ..review.skills import SkillBundle, SkillsUnavailable, bundles_for_cohort
from ..runtimes.client import ModelClient, ModelError, ToolCallTruncated
from .agent_tools import TOOL_SCHEMAS, ToolLog, Toolbox

MAX_TOOL_TURNS = 24

# A tool call that writes a file carries the whole file as a JSON string, so the
# budget has to fit the file and its escaping — not just the model's prose. At
# 3000 this truncated a perfectly good four-case test file mid-string, and the
# server reported it as a parse error at column 680.
# Enough for a write_file call carrying a six-thousand-character file plus its
# JSON escaping, and no more. A larger budget is not headroom — it is an
# invitation to answer in prose instead of acting, and a turn spent that way
# costs four hundred seconds at thirty tokens a second.
IMPLEMENTER_MAX_TOKENS = 4000

# Two replies running with no tool call is not a model that needs another nudge;
# it is a model that is not going to use the tools. Stopping says so while the
# evidence is still legible, instead of burning twenty more turns to reach the
# same conclusion.
MAX_SILENT_TURNS = 2

# A ceiling stated to the model, well under what the budget can carry. A tool
# call is one JSON string containing the whole file plus its escaping, so a file
# that outgrows the budget is not a large file — it is a failed turn.
MAX_WRITE_CHARS = 6000

# What counts as having observed a failing test. See the note in `implement`.
RED_EXIT_CODES = frozenset({1, 2})
EMPTY_SUITE_EXIT = 5


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
        "2. Call run_tests. It MUST show the test not passing — pytest exit "
        "code 1 (ran and failed) or 2 (collection error, which is what you get "
        "when the test imports a module you have not written yet). Exit code 5 "
        "means no tests were collected at all, which is an empty suite and not "
        "a red phase; if you see 5, your test file is missing or is not named "
        "test_*.py. A test that passes before the implementation exists is "
        "testing nothing, and you must fix the test rather than continue.\n"
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
    # The pinned Superpowers skills each role is held to, keyed by role.
    #
    # Empty means the agents run on this harness's own prose, which is a fact a
    # run must record rather than paper over: the discipline would then be
    # unversioned and could drift without anyone noticing.
    skills: dict = field(default_factory=dict)
    # Set when a role has been handed over mid-package. Prepended to that role's
    # next prompt so the replacement knows what it inherited.
    briefings: dict = field(default_factory=dict)
    # A bounded, code-shaped slice of the package to actually build.
    #
    # The commissioning plan's deliverables are documents and schemas — it
    # describes what must exist, and the implementation lives in the target
    # repository. Test-first discipline needs something a test can fail against,
    # so a live run names one executable slice and says so in the report. The
    # packet still supplies every constraint, forbidden action and acceptance
    # criterion the reviewers judge against; what is narrowed is the build, not
    # the rules. A run with a focus has characterised the harness, not completed
    # the package, and the result records the difference.
    focus: str | None = None

    def system_prompt(self, role: str) -> str:
        """The role card, preceded by the discipline the role is held to.

        Order matters: the skill comes first and says it wins on method, so a
        role card and a skill that disagree resolve towards the pinned artefact
        rather than towards whichever the model read last.
        """
        card = ROLE_CARDS[role]
        bundle = self.skills.get(role)
        prompt = card if bundle is None else (
            f"{bundle.text}\n\n---\n\n# Your role\n\n{card}")
        briefing = self.briefings.get(role)
        if briefing:
            prompt = f"{prompt}\n\n---\n\n{briefing}"
        return prompt

    def rebind(self, role: str, runtime_id: str, handoff) -> None:
        """Point a role at another runtime, and tell the replacement what it is
        taking over. The briefing is task state; the previous agent's
        conversation is dropped rather than forwarded."""
        from .live import ENDPOINTS
        endpoint = ENDPOINTS.get(runtime_id)
        if endpoint:
            self.clients[role] = ModelClient(endpoint, model="local")
        self.briefings[role] = handoff.briefing()
        # The replacement starts clean. Keeping the old transcript under the
        # same key would leak it into the next _record and, worse, invite
        # someone to feed it back in.
        self.transcripts.pop(role, None)

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
            {"role": "system", "content": self.system_prompt("architect")},
            {"role": "user", "content":
             _packet_brief(packet) + "\n\n"
             f"Assurance level: {cohort.assurance_level}.\n\n"
             + (f"The bounded, executable slice to plan for is:\n{self.focus}\n\n"
                if self.focus else "")
             + "Produce an implementation plan as JSON with keys: summary (one "
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
        """A worktree for this attempt.

        The id used to be the packet digest alone, so every attempt at the same
        packet asked for the same path — and the manager refuses that, rightly,
        so that two runs cannot share a tree. The effect was that a package
        could be commissioned exactly once and then never again until somebody
        removed a directory by hand.

        The digest stays, because it says which packet this tree belongs to and
        makes it findable. What is added is which attempt: the trees a previous
        attempt left are evidence of that attempt and are not reused or
        silently deleted.
        """
        base = f"live-{packet.packet_sha256[:8]}"
        # `git worktree list --porcelain` names the path under the key
        # "worktree". Reading it as "path" raised KeyError inside the very step
        # this was meant to fix, and the run failed at `worktree` again — with a
        # different message, which is the only reason it was not mistaken for
        # the original bug.
        known = {tree.get("worktree") for tree in self.worktrees.list()}
        attempt, task_id = 1, base
        while True:
            path = self.worktrees.worktree_root / f"{packet.wp_id}__{task_id}"
            if not path.exists() and str(path) not in known:
                break
            attempt += 1
            task_id = f"{base}-{attempt}"
        return self.worktrees.create(task_id, packet.wp_id)

    def implement(self, packet: WPPacket, plan: dict, worktree,
                  findings: list[dict] | None = None) -> dict:
        """Drive the implementer until a red run and then a green run exist.

        `findings` are what a reviewer said was wrong with the last candidate.
        Without them a retry is the same attempt run again: the harness kept
        the findings and the implementer never saw one, so WP-001 produced a
        candidate missing the same two deliverables twice.
        """
        client = self._client("implementer")
        log = ToolLog()
        tools = Toolbox(worktree.path, log)
        root = Path(worktree.path)

        # The worktree listing is handed over rather than discovered. A turn
        # spent on list_files is a turn not spent writing, and at roughly a
        # minute a turn against a six-thousand-token prompt that is not a
        # rounding error.
        existing = sorted(
            str(f.relative_to(root)) for f in root.rglob("*")
            if f.is_file() and ".git" not in f.parts)

        messages = [
            {"role": "system", "content": self.system_prompt("implementer")},
            {"role": "user", "content":
             _packet_brief(packet, limit=3500) + "\n\n"
             "## the accepted plan\n" + json.dumps(plan, indent=2)[:2000] + "\n\n"
             + (f"## build exactly this, and nothing more\n{self.focus}\n\n"
                if self.focus else "")
             + (("## what a reviewer rejected in the last attempt\n"
                 + "\n".join(f"- [{f.get('severity', 'HIGH')}] {f.get('summary', '')}"
                              for f in findings[:12])
                 + "\n\nEach of these must be answered by this candidate.\n\n")
                if findings else "")
             + "## the worktree already contains\n"
             + ("\n".join(f"- {f}" for f in existing[:60]) or "- (nothing)")
             + "\n\nYou do not need to list or read these unless you intend to "
               "change one. Work only through the tools, and begin by writing "
               "the failing test with write_file. When run_tests has shown the "
               "test not passing and then passing, reply with the single word "
               "DONE and nothing else."}]

        red_exit: int | None = None
        green_exit: int | None = None
        silent = 0
        for turn in range(MAX_TOOL_TURNS):
            # Truncation mid tool call is a statement about the budget, not
            # about the model: the arguments were being written correctly and
            # the count ran out. Failing the run there blamed the implementer
            # for a file that was merely long, and both runtimes died the same
            # way on the same turn. Retry the turn once with room, and if it
            # truncates again say so — a file that will not fit twice the
            # budget wants splitting, which the model is told to do.
            try:
                reply = client.chat(messages, tools=TOOL_SCHEMAS,
                                    max_tokens=IMPLEMENTER_MAX_TOKENS)
            except ToolCallTruncated:
                reply = client.chat(
                    messages + [{"role": "user", "content":
                                 "Your last tool call was cut off because it "
                                 "was too long. Write the same file in smaller "
                                 "pieces: one write_file per file, and split a "
                                 "long file into a first write_file and then "
                                 "append_file calls."}],
                    tools=TOOL_SCHEMAS,
                    max_tokens=IMPLEMENTER_MAX_TOKENS * 2)
            if not reply.tool_calls:
                text = (reply.content or "").strip()
                if "DONE" in text.upper() and green_exit == 0:
                    break
                silent += 1
                if silent >= MAX_SILENT_TURNS:
                    raise ImplementationRefused(
                        f"the implementer answered in prose {silent} turns "
                        f"running without calling a tool (last reply "
                        f"{len(text)} chars, finish={reply.finish_reason}). "
                        "It is describing the work rather than doing it.")
                messages.append({"role": "assistant", "content": text[:2000]})
                messages.append({"role": "user", "content":
                    "Continue using the tools. "
                    + ("Write the test file with write_file, then call "
                       "run_tests. It must show the test not passing — exit "
                       "code 1 or 2. Exit code 5 means no test file was found."
                       if red_exit is None else
                       "You have a failing test. Write the implementation with "
                       "write_file, then call run_tests again."
                       if green_exit != 0 else
                       "Reply DONE.")})
                continue

            silent = 0
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
                    code = result["exit_code"]
                    # pytest's exit codes are not a boolean, and the
                    # distinction that matters is "no test exists" versus "a
                    # test exists and does not pass".
                    #
                    #   0 — passed
                    #   1 — tests ran and failed
                    #   2 — collection error, which for test-first work is the
                    #       *normal* first red: the test imports a module that
                    #       does not exist yet
                    #   5 — no tests collected: an empty suite
                    #
                    # Both narrow readings are wrong. Accepting any non-zero
                    # code lets 5 count as red, so an implementer can claim a
                    # cycle it never performed by calling run_tests before
                    # writing anything. Accepting only 1 rejects 2, which is
                    # what a correct test-first cycle actually produces first —
                    # and the loop then never terminates while the model
                    # correctly reports it is done.
                    if code == EMPTY_SUITE_EXIT:
                        result["harness_note"] = (
                            "No tests were collected. That is an empty suite, "
                            "not a failing test. Write the test file first, "
                            "named test_*.py.")
                    elif red_exit is None and code in RED_EXIT_CODES:
                        red_exit = code
                        result["harness_note"] = (
                            "Red phase observed. Now write the smallest "
                            "implementation that makes it pass.")
                    elif red_exit is not None and code == 0:
                        green_exit = 0
                        result["harness_note"] = "Green. Reply DONE."
                    elif red_exit is None and code == 0:
                        result["harness_note"] = (
                            "The suite passed before any implementation exists. "
                            "That test is not testing the required behaviour. "
                            "Fix the test so it fails first.")
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": json.dumps(result)[:3000]})
            if green_exit == 0 and red_exit is not None:
                break

        self._record("implementer", messages, f"red={red_exit} green={green_exit}")
        discipline_dir = self.evidence_dir / packet.wp_id
        discipline_dir.mkdir(parents=True, exist_ok=True)
        (discipline_dir / "tool_log.json").write_text(
            json.dumps({"calls": log.calls}, indent=2))
        (discipline_dir / "skills_injected.json").write_text(json.dumps(
            {"schema": "dume.skills_injected/1",
             "note": ("What each role was actually held to. Injection is an "
                      "input, not proof of obedience — that is answered by the "
                      "red-then-green exit codes and the independent reviews."),
             "bundles": {r: b.as_dict() for r, b in self.skills.items()}},
            indent=2))

        if red_exit is None or green_exit != 0:
            raise ImplementationRefused(
                f"no red-then-green cycle was observed "
                f"(red={red_exit}, green={green_exit}) after "
                f"{len(log.calls)} tool call(s)")

        subprocess.run(["git", "-C", str(root), "add", "-A"],
                       capture_output=True, check=False)
        # The author is the agent that wrote it, named per commit rather than
        # configured globally: the repository is not any one agent's, and a
        # commit that says who produced it is the difference between a
        # candidate and an anonymous change.
        producer = getattr(self.bindings.get("implementer"), "agent_id",
                           f"{packet.wp_id}/implementer")
        commit = subprocess.run(
            ["git", "-C", str(root),
             "-c", f"user.name={producer}",
             "-c", "user.email=dume@aethrionis.local",
             "commit", "-qm", f"{packet.wp_id}: candidate"],
            capture_output=True, text=True, check=False)
        # Not swallowed. This ran with check=False and no output, so a commit
        # that failed for want of an identity produced a candidate silently
        # equal to the base — six files written, tests red then green, and
        # nothing recorded. Every run failed the same way and the message named
        # the diff rather than the commit.
        if commit.returncode != 0:
            raise ImplementationRefused(
                "the work was written but could not be committed: "
                + (commit.stderr or commit.stdout or "git said nothing"
                   ).strip().splitlines()[0][:200])
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
            {"role": "system", "content": self.system_prompt(role)},
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
            {"role": "system", "content": self.system_prompt("verifier")},
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
