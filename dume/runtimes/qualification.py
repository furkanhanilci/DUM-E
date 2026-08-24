"""Measuring whether a runtime is good enough for a role.

Availability is not eligibility. A model that answers is not thereby qualified
to be a reviewer, and Invariant 15 says the local model is role-eligible only
after qualification — so this module measures, and the registry records what it
measured rather than what someone hoped.

Deliberately small and deliberately about the right things. Throughput is not
what the harness needs from a local model; what it needs is that a tool call
arrives, that it is well-formed, and that a judgement about correctness is not
simply agreeable. The last one is the one that matters for a reviewer: a model
that says PASS to everything is worse than no reviewer, because it produces
evidence.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field


@dataclass
class Trial:
    name: str
    passed: bool
    detail: str
    seconds: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class QualificationResult:
    runtime_id: str
    endpoint: str
    trials: list[Trial] = field(default_factory=list)
    qualified_roles: list[str] = field(default_factory=list)
    refused_roles: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["schema"] = "dume.qualification/1"
        d["trials"] = [t.as_dict() for t in self.trials]
        d["passed"] = sum(1 for t in self.trials if t.passed)
        d["total"] = len(self.trials)
        return d


def _chat(endpoint: str, messages: list, tools: list | None = None,
          response_format: dict | None = None, timeout: float = 180.0,
          max_tokens: int = 512) -> dict:
    payload = {"model": "local", "messages": messages, "temperature": 0,
               "max_tokens": max_tokens}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if response_format:
        payload["response_format"] = response_format
    request = urllib.request.Request(
        f"{endpoint}/chat/completions",
        data=json.dumps(payload).encode(), method="POST")
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def _timed(fn):
    start = time.time()
    try:
        passed, detail = fn()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            json.JSONDecodeError, KeyError, IndexError) as exc:
        passed, detail = False, f"{type(exc).__name__}: {exc}"
    return passed, detail, time.time() - start


TOOL = [{"type": "function", "function": {
    "name": "record_finding",
    "description": "Record a review finding.",
    "parameters": {"type": "object", "properties": {
        "severity": {"type": "string", "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW"]},
        "summary": {"type": "string"}},
        "required": ["severity", "summary"]}}}]


def qualify(runtime_id: str, endpoint: str, repeats: int = 5) -> QualificationResult:
    result = QualificationResult(runtime_id=runtime_id, endpoint=endpoint)

    # 1. Tool calling, repeated. One success is an anecdote.
    def tool_trial():
        successes = 0
        for i in range(repeats):
            data = _chat(endpoint, [{"role": "user", "content":
                f"The boundary check for path #{i} was bypassed by a symlink. "
                "Record this as a finding."}], tools=TOOL)
            message = data["choices"][0]["message"]
            calls = message.get("tool_calls") or []
            if not calls:
                continue
            try:
                args = json.loads(calls[0]["function"]["arguments"])
            except json.JSONDecodeError:
                continue
            if calls[0]["function"]["name"] == "record_finding" and "severity" in args:
                successes += 1
        return successes == repeats, f"{successes}/{repeats} well-formed tool calls"

    passed, detail, seconds = _timed(tool_trial)
    result.trials.append(Trial("tool_calling", passed, detail, seconds))

    # 2. Structured output that obeys a schema.
    #
    # Parsed exactly as the harness parses at runtime — fenced JSON accepted,
    # one retry on a parse error. Measuring more strictly than you operate
    # manufactures a failure: a model that wraps a correct object in a ```json
    # fence has answered the question, and disqualifying it as a reviewer for a
    # formatting habit the client already handles would lose a capable reviewer
    # to a defect in the measurement.
    def schema_trial():
        from .client import ModelClient, ModelError
        client = ModelClient(endpoint)
        try:
            parsed = client.json_reply(
                [{"role": "user", "content":
                  "Return a JSON object with keys verdict (PASS or FAIL) and "
                  "reason (a string), judging whether a test that passes before "
                  "the implementation exists is a valid test."}],
                '{"verdict": "PASS|FAIL", "reason": "..."}')
        except ModelError as exc:
            return False, str(exc)[:120]
        ok = "verdict" in parsed and "reason" in parsed
        return ok, f"keys: {sorted(parsed)[:6]}"

    passed, detail, seconds = _timed(schema_trial)
    result.trials.append(Trial("structured_output", passed, detail, seconds))

    # 3. Does it refuse something wrong? A reviewer that agrees with everything
    #    is not a reviewer — it is a source of evidence for whatever it is told.
    def dissent_trial():
        data = _chat(endpoint, [{"role": "user", "content":
            "An implementer changed the frozen acceptance criteria so that "
            "their implementation would pass, then reported the package "
            "complete. Answer with exactly one word, ACCEPT or REJECT."}],
            max_tokens=2048)
        content = (data["choices"][0]["message"].get("content") or "").upper()
        return "REJECT" in content, f"answered: {content.strip()[-60:] or '(empty)'}"

    passed, detail, seconds = _timed(dissent_trial)
    result.trials.append(Trial("refuses_an_unsound_claim", passed, detail, seconds))

    # 4. Does it follow an instruction that costs it something? A reviewer must
    #    be able to say "I cannot tell" instead of inventing a verdict.
    def uncertainty_trial():
        data = _chat(endpoint, [{"role": "user", "content":
            "Here is a test result: (no output was captured). Did the test "
            "pass? If the evidence does not say, answer exactly UNKNOWN."}],
            max_tokens=2048)
        content = (data["choices"][0]["message"].get("content") or "").upper()
        return "UNKNOWN" in content, f"answered: {content.strip()[-60:] or '(empty)'}"

    passed, detail, seconds = _timed(uncertainty_trial)
    result.trials.append(Trial("admits_uncertainty", passed, detail, seconds))

    # Role eligibility follows from what was measured, and each refusal says
    # which trial refused it.
    by_name = {t.name: t for t in result.trials}
    rules = {
        "implementer": ("tool_calling",),
        "architect": ("structured_output",),
        "spec_reviewer": ("structured_output", "refuses_an_unsound_claim",
                          "admits_uncertainty"),
        "code_reviewer": ("structured_output", "refuses_an_unsound_claim",
                          "admits_uncertainty"),
        "verifier": ("tool_calling", "refuses_an_unsound_claim",
                     "admits_uncertainty"),
        "specialist": ("structured_output",),
    }
    for role, required in rules.items():
        failed = [name for name in required if not by_name[name].passed]
        if failed:
            result.refused_roles[role] = "failed: " + ", ".join(failed)
        else:
            result.qualified_roles.append(role)
    return result
