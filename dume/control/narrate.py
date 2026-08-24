"""Saying what an answer means, without adding anything to it.

A command answers with numbers and states. That is the right thing for a
command, and the wrong thing for somebody who asked "durum nedir" — they wanted
to know how it is going, and a table is not an answer to that.

So a local model is asked to read the output and say what it means, and its
sentences are shown *under* the output rather than instead of it. The numbers
stay; the prose sits with them.

The one rule the model is held to is that it may not add anything. It is given
the command's output and told to explain only what is there — no advice, no
guesses about causes, no numbers that are not in front of it. A narration that
invents a fact is worse than no narration, because it arrives in the same
message as facts that are true.

This is decoration on a read. It never runs for a CONTROL, HUMAN_DECISION or
DANGEROUS_ACTION command: those answers are records of something that happened,
and a model paraphrasing them would put a second account of an action next to
the real one.
"""
from __future__ import annotations

INSTRUCTION = """You read one command's output and tell the person what it means.

They can already see the numbers. Do not repeat them. Tell them the thing the
numbers imply that they would otherwise have to work out — which item is
holding up the others, what state the work is in, what the output says is
blocking it.

Rules, in order of importance:
1. Say only what the output supports. Never add a number, a name, a cause or a
   recommendation that is not in it. If the output does not say why something
   failed, do not say why.
2. Two or three sentences. Plain language, no lists, no headings.
3. Answer in the language the question was asked in.
4. Never begin with "The output shows" or "Bu çıktı" — say the thing itself.

You are describing a record. You decide nothing, and nothing you say changes
any state."""

# Only readings get narrated. See the module docstring.
NARRATABLE = frozenset({
    "status", "next", "show", "history", "findings", "evidence", "runtimes",
    "roles", "open", "spaces", "read"})


def narrate(question: str, command: str, output: str,
            *, timeout: float = 20.0) -> str | None:
    """A sentence or two about what the output means, or None.

    None on any failure, and the caller shows the output alone. The command
    already answered; a model being slow or unreachable must not turn a working
    answer into an error.

    Fast enough to arrive while the reader is still looking: the thinking
    phase is off, and without it a local 27B answers in about two seconds.
    """
    verb = command.split()[0] if command else ""
    if verb not in NARRATABLE or not output.strip():
        return None
    try:
        from ..runtimes.client import ModelClient
        from ..runtimes.profiles import RuntimeRegistry
        from .live import ENDPOINTS

        # Whichever local runtime is up. A runtime record says whether it is
        # available; where to reach it lives with the server profiles, and
        # reading the endpoint off the record would have been a field that is
        # not there.
        registry = RuntimeRegistry.load()
        rows = registry.runtimes
        rows = rows() if callable(rows) else rows
        endpoint = next(
            (ENDPOINTS[rid] for rid, r in rows.items()
             if rid in ENDPOINTS and getattr(r, "status", "") == "AVAILABLE"),
            None)
        if endpoint is None:
            return None
        client = ModelClient(endpoint, model="local", timeout=timeout)
        reply = client.chat(
            [{"role": "system", "content": INSTRUCTION},
             {"role": "user",
              "content": f"They asked: {question}\n\n"
                         f"The command `{command}` answered:\n\n{output[:2500]}"}],
            # No thinking. This is a two-sentence paraphrase of text already
            # on screen, and the reasoning budget is what made it take over a
            # minute — long enough that the reader had moved on, and often
            # long enough to return an empty string having spent the budget
            # before writing a word. Without it: two seconds.
            max_tokens=400, think=False)
    except Exception:
        # Deliberately silent. The command answered; this is decoration, and a
        # failure here that surfaced as an error would make a working answer
        # look broken.
        return None

    # The client returns a Reply, not a string — it carries the tool calls and
    # the finish reason as well as the text, because the executor needs those.
    text = (getattr(reply, "text", None) or getattr(reply, "content", "") or "").strip()
    return text or None


ANSWER = """Somebody asked you a question about the work you are running.

You are given the current state of the work. Answer their question from it, in
the language they asked in, the way a colleague would across a desk.

Rules, in order of importance:
1. Say only what the state in front of you supports. If it does not answer the
   question, say plainly that you do not have it — never fill the gap.
2. You decide nothing and you change nothing. If they are asking you to do
   something rather than to explain something, say which command does it.
3. Three or four sentences. Plain language, no headings.

You are reading a record, not writing one."""


def converse(question: str, state: str, *, timeout: float = 25.0) -> str | None:
    """Answer a free-form question from the current state.

    A sentence that matches no command used to be refused with the whole
    vocabulary printed underneath. That is the right answer to `sudo rm`, and
    the wrong one to "ne durumda" — a person asking a question in their own
    words got a list of words they were allowed to use instead.

    This never runs anything. It reads the state it was handed and talks about
    it, which is why an unmatched sentence can be answered without any of the
    authority a command would need.
    """
    if not question.strip():
        return None
    try:
        from ..runtimes.client import ModelClient
        from ..runtimes.profiles import RuntimeRegistry
        from .live import ENDPOINTS

        registry = RuntimeRegistry.load()
        rows = registry.runtimes
        rows = rows() if callable(rows) else rows
        endpoint = next(
            (ENDPOINTS[rid] for rid, r in rows.items()
             if rid in ENDPOINTS and getattr(r, "status", "") == "AVAILABLE"),
            None)
        if endpoint is None:
            return None
        client = ModelClient(endpoint, model="local", timeout=timeout)
        reply = client.chat(
            [{"role": "system", "content": ANSWER},
             {"role": "user",
              "content": f"They asked: {question}\n\n"
                         f"The work right now:\n\n{state[:3000]}"}],
            max_tokens=500, think=False)
    except Exception:
        return None

    text = (getattr(reply, "text", None) or getattr(reply, "content", "") or "").strip()
    return text or None
