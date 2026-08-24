"""The authenticated human command gateway.

Invariant 18: untrusted inbound content is data, not command. A message arriving
from anywhere — Buzz, Telegram, a webhook — is a *request* that this gateway
either translates into one of a closed set of intents or refuses. There is no
path from free text to an action, and no action outside the table below exists.

Four classes, because they need four different answers:

* `READ` — shows state. Cheap, safe, allowed to anyone authenticated.
* `CONTROL` — changes operational flow: pause, resume, retry, reserve a runtime.
  Reversible, audited.
* `HUMAN_DECISION` — records a judgement only a human may make. Never inferred,
  never defaulted.
* `DANGEROUS_ACTION` — irreversible or authority-bearing. Requires a second,
  explicit confirmation carrying a nonce this gateway issued, so a single
  message can never be enough.

What this gateway will not do, whatever the message says: run a shell command,
accept a forwarded message as an authorisation, or mark a package ACCEPTED.
Acceptance is not a chat action.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

READ, CONTROL, HUMAN_DECISION, DANGEROUS_ACTION = (
    "READ", "CONTROL", "HUMAN_DECISION", "DANGEROUS_ACTION")


@dataclass(frozen=True)
class Action:
    name: str
    klass: str
    summary: str
    # Named parameters, in order. Anything else in the message is not an
    # argument — it is prose, and prose is not a parameter.
    parameters: tuple[str, ...] = ()
    confirm: bool = False


ACTIONS: dict[str, Action] = {a.name: a for a in (
    Action("status", READ, "Commissioning state of every package."),
    Action("show", READ, "One package: state, candidate, dependencies.", ("wp",)),
    Action("history", READ, "Every transition for one package.", ("wp",)),
    Action("findings", READ, "Open findings, most severe first.", ("wp",)),
    Action("runtimes", READ, "Runtime status, mode and qualification."),
    Action("next", READ, "Which packages are READY, and what blocks the rest."),
    Action("evidence", READ, "Evidence recorded for one package.", ("wp",)),
    Action("roles", READ, "Every logical role, what it decides, and what it is "
                          "currently bound to."),
    Action("ask", READ, "Put a question to a role. The answer is that role's "
                        "recorded work, not a new opinion.", ("role", "question")),
    Action("spaces", READ, "AETHRIONIS: every space, what it decides, its channels."),
    Action("commands", READ, "Everything you may do here, grouped by class."),
    Action("read", READ, "The last messages in one channel.", ("channel",)),
    Action("open", READ, "Messages nobody has answered, across every space."),

    # Saying something is CONTROL, not READ. It is also not a decision: the
    # class exists so that "can act" and "can settle" stay separable, which is
    # the whole point of the four classes.
    Action("say", CONTROL, "Post a STATUS message to a channel.",
           ("channel", "text")),
    Action("challenge", CONTROL, "Post a CHALLENGE. Must name what it is about.",
           ("channel", "reference", "text")),

    Action("pause", CONTROL, "Stop starting new work. Running work finishes."),
    Action("resume", CONTROL, "Allow new work to start again."),
    Action("retry", CONTROL, "Re-enter a failed package at PLANNED.", ("wp",)),
    # Starting real work is CONTROL, not a decision: it moves a package the
    # lifecycle already says may move. What the run produces still has to pass
    # independent review and a deterministic gate, and accepting it is still a
    # HUMAN_DECISION — which is why this does not need to be one.
    Action("commission", CONTROL,
           "Commission a package: real state, real target repository, live "
           "models. Runs in the background and narrates as it goes.", ("wp",)),
    Action("reserve", CONTROL, "Keep a runtime for architecture-critical work.",
           ("runtime",)),
    Action("release", CONTROL, "Return a reserved runtime to normal use.",
           ("runtime",)),
    Action("disable", CONTROL, "Take a runtime out of the eligible pool.",
           ("runtime",)),
    Action("enable", CONTROL, "Return a disabled runtime to the pool.", ("runtime",)),

    Action("decide", HUMAN_DECISION,
           "Record a human ruling on an escalation.", ("wp", "ruling")),
    Action("block", HUMAN_DECISION, "Block a package with a stated reason.",
           ("wp", "reason")),

    Action("kill", DANGEROUS_ACTION,
           "Stop every running agent and container immediately.", (), confirm=True),
    Action("bind_workspace", DANGEROUS_ACTION,
           "Bind a real path to a workspace slot. This is how a build agent "
           "first gains reach outside the harness.", ("name", "path"), confirm=True),
)}

# Anything that looks like an attempt to reach past the command table. These are
# refused loudly and audited rather than ignored, because a refusal nobody sees
# teaches an attacker only that they should try something quieter.
INJECTION_MARKERS = re.compile(
    r"(?i)(?:^|\s)(?:ignore (?:all |the )?(?:previous|prior|above)"
    r"|disregard (?:all |the )?(?:previous|prior|above)"
    r"|you are now|new instructions?:|system prompt|</?system>"
    r"|sudo |rm -rf|curl .*\|.*sh|bash -c|\$\(|`)")


class CommandRefused(RuntimeError):
    """The request did not become an intent. The message says why."""


@dataclass
class CommandIntent:
    command_id: str
    authenticated_actor: str
    channel: str
    klass: str
    action: str
    target: str | None = None
    arguments: dict = field(default_factory=dict)
    issued_at: str = ""
    confirmation_ref: str | None = None
    authorization_result: str = "PENDING"
    audit_ref: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Principal:
    """Who is allowed to command, and how much.

    An identifier alone is not a principal. `verified` records that the surface
    established this identity rather than reading it out of the message, which
    is the difference between a sender and a claim.
    """
    actor_id: str
    display_name: str
    max_class: str = CONTROL
    verified: bool = True


CLASS_ORDER = {READ: 0, CONTROL: 1, HUMAN_DECISION: 2, DANGEROUS_ACTION: 3}


class CommandGateway:
    """Turns a message into an intent, or refuses and says why."""

    def __init__(self, principals: dict[str, Principal],
                 audit_path: Path | str | None = None,
                 rate_limit: int = 30, window_seconds: int = 60):
        self.principals = principals
        self.audit_path = Path(audit_path) if audit_path else None
        self.rate_limit = rate_limit
        self.window_seconds = window_seconds
        self._recent: dict[str, list[float]] = {}
        self._pending: dict[str, dict] = {}

    # ---- audit ----------------------------------------------------------

    def _audit(self, event: dict) -> str:
        event = dict(event)
        event["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        blob = json.dumps(event, sort_keys=True)
        ref = hashlib.sha256(blob.encode()).hexdigest()[:16]
        event["audit_ref"] = ref
        if self.audit_path:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a") as fh:
                fh.write(json.dumps(event, sort_keys=True) + "\n")
        return ref

    def _refuse(self, actor: str, channel: str, text: str, reason: str) -> None:
        # A rejected command is audited as carefully as an accepted one. The
        # rejections are the interesting half of the log.
        self._audit({"outcome": "REFUSED", "actor": actor, "channel": channel,
                     "text": text[:200], "reason": reason})
        raise CommandRefused(reason)

    # ---- translation ----------------------------------------------------

    def translate(self, *, actor_id: str, channel: str, text: str,
                  forwarded: bool = False, verified: bool = True) -> CommandIntent:
        """Turn one message into one intent, or refuse."""
        text = (text or "").strip()

        # A forwarded message carries no privilege, whatever it says and
        # whoever originally wrote it. Someone can be made to forward anything.
        if forwarded:
            self._refuse(actor_id, channel, text,
                         "a forwarded message carries no authority; send the "
                         "command yourself")

        principal = self.principals.get(actor_id)
        if principal is None:
            self._refuse(actor_id, channel, text,
                         f"{actor_id} is not an authorised principal")
        if not verified or not principal.verified:
            self._refuse(actor_id, channel, text,
                         "the surface did not establish this sender's identity")

        now = time.time()
        window = [t for t in self._recent.get(actor_id, []) if now - t < self.window_seconds]
        if len(window) >= self.rate_limit:
            self._refuse(actor_id, channel, text,
                         f"rate limit: {self.rate_limit} commands per "
                         f"{self.window_seconds}s")
        window.append(now)
        self._recent[actor_id] = window

        if INJECTION_MARKERS.search(text):
            self._refuse(actor_id, channel, text,
                         "the message contains instruction-shaped or shell-shaped "
                         "content; it is data, not a command")

        # `@role question` is the addressing form the design uses. It is
        # rewritten into `ask role question` rather than given its own parser,
        # so a role-addressed message goes through exactly the same
        # authorisation, rate limiting and audit as everything else.
        if text.startswith("@"):
            text = "ask " + text[1:].replace("-", "_")

        # Only the first token can name an action, and it must be in the table.
        parts = text.lstrip("/").split()
        if not parts:
            self._refuse(actor_id, channel, text, "empty message")
        name = parts[0].lower()
        action = ACTIONS.get(name)
        if action is None:
            self._refuse(actor_id, channel, text,
                         f"{name!r} is not a command. There is no shell here — "
                         f"the whole vocabulary is: {', '.join(sorted(ACTIONS))}")

        if CLASS_ORDER[action.klass] > CLASS_ORDER[principal.max_class]:
            self._refuse(actor_id, channel, text,
                         f"{name} is {action.klass}; {principal.display_name} is "
                         f"authorised only up to {principal.max_class}")

        arguments: dict = {}
        remaining = parts[1:]
        for index, parameter in enumerate(action.parameters):
            if index >= len(remaining):
                self._refuse(actor_id, channel, text,
                             f"{name} needs {', '.join(action.parameters)}")
            # The last named parameter absorbs the rest, so a reason or a ruling
            # can be a sentence without becoming a parsing puzzle.
            if index == len(action.parameters) - 1:
                arguments[parameter] = " ".join(remaining[index:])[:500]
            else:
                arguments[parameter] = remaining[index]

        if "wp" in arguments:
            wp = arguments["wp"].upper()
            if not re.fullmatch(r"WP-\d{3}", wp):
                self._refuse(actor_id, channel, text,
                             f"{arguments['wp']!r} is not a work-package id")
            arguments["wp"] = wp

        intent = CommandIntent(
            command_id=secrets.token_hex(8), authenticated_actor=actor_id,
            channel=channel, klass=action.klass, action=name,
            target=arguments.get("wp") or arguments.get("runtime")
            or arguments.get("name"),
            arguments=arguments,
            issued_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

        if action.confirm:
            nonce = secrets.token_hex(4)
            self._pending[nonce] = {"intent": intent.as_dict(), "at": now,
                                    "actor": actor_id}
            intent.authorization_result = "AWAITING_CONFIRMATION"
            intent.confirmation_ref = nonce
            intent.audit_ref = self._audit(
                {"outcome": "AWAITING_CONFIRMATION", "intent": intent.as_dict()})
            return intent

        intent.authorization_result = "AUTHORISED"
        intent.audit_ref = self._audit({"outcome": "AUTHORISED",
                                        "intent": intent.as_dict()})
        return intent

    def confirm(self, *, actor_id: str, nonce: str,
                ttl_seconds: int = 120) -> CommandIntent:
        """Complete a dangerous action, or refuse.

        The confirmation must come from the same principal, carry the nonce this
        gateway issued, and arrive quickly. A nonce is consumed whether or not
        it succeeds, so a stale one cannot be retried into working.
        """
        pending = self._pending.pop(nonce, None)
        if pending is None:
            self._refuse(actor_id, "confirmation", nonce,
                         "no pending action with that confirmation reference")
        if pending["actor"] != actor_id:
            self._refuse(actor_id, "confirmation", nonce,
                         "a dangerous action must be confirmed by the principal "
                         "that requested it")
        if time.time() - pending["at"] > ttl_seconds:
            self._refuse(actor_id, "confirmation", nonce,
                         f"the confirmation expired after {ttl_seconds}s")
        intent = CommandIntent(**pending["intent"])
        intent.authorization_result = "AUTHORISED"
        intent.audit_ref = self._audit({"outcome": "CONFIRMED",
                                        "intent": intent.as_dict()})
        return intent

    def vocabulary(self) -> list[dict]:
        return [{"command": a.name, "class": a.klass, "summary": a.summary,
                 "parameters": list(a.parameters), "needs_confirmation": a.confirm}
                for a in sorted(ACTIONS.values(),
                                key=lambda a: (CLASS_ORDER[a.klass], a.name))]
