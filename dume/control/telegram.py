"""The Telegram control bridge.

Telegram is a convenience surface, not an authority. Every message it delivers
goes through the same command gateway as anything else, and the gateway's answer
is final — this module's only jobs are to establish *who sent it*, to notice a
forwarded message, and to render the result.

The three failure modes WP-046 names, each handled as a mechanism rather than a
warning:

* **Bot token leaked.** The token never enters the repository, a packet, an
  evidence file or a log line. It lives in the key store on a filesystem that
  can enforce a mode, and this module redacts it from every error it raises —
  including the ones the HTTP library puts it in, because the token is in the
  URL.
* **Any group member can control it.** Only allowlisted user ids are principals,
  and the id is taken from Telegram's `from` field, never from the message text.
  Adding the bot to a group does not enfranchise the group.
* **A forwarded message triggers privilege.** Any message carrying forward
  metadata is refused before it reaches the gateway. Someone can be persuaded
  to forward anything.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .command_gateway import CommandGateway, CommandRefused, Principal

API = "https://api.telegram.org"
SECRETS = Path.home() / ".dume" / "secrets" / "telegram.json"


class TelegramError(RuntimeError):
    """A Telegram API call failed. The token is never in the message."""


def _redact(text: str, token: str) -> str:
    if token:
        text = text.replace(token, "«REDACTED:telegram_bot_token»")
        # The token's id half is enough to identify the bot, so it goes too.
        head = token.split(":")[0]
        if head:
            text = re.sub(rf"\b{re.escape(head)}:[A-Za-z0-9_\-]+", "«REDACTED»", text)
    return text


@dataclass
class Config:
    token: str
    allowed: dict[str, dict] = field(default_factory=dict)
    poll_timeout: int = 25
    # Where the harness narrates. A run that only answers when asked makes the
    # operator poll a machine that already knows when something happened; the
    # one thing a phone is genuinely better at than a desktop is being told.
    #
    # Separate from `allowed` because they are different questions: who may
    # drive this, and where it speaks. A broadcast chat is not thereby
    # authorised to command anything — narration goes out, commands still come
    # from a principal.
    broadcast: str | None = None

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Config":
        path = Path(path) if path else SECRETS
        if not path.is_file():
            raise TelegramError(
                f"no Telegram configuration at {path}. Create it with mode 0600 "
                'as {"token": "<bot token>", "allowed": {"<user id>": '
                '{"name": "...", "max_class": "CONTROL"}}}')
        data = json.loads(path.read_text())
        if not data.get("token"):
            raise TelegramError(f"{path} has no token")
        return cls(token=data["token"], allowed=data.get("allowed") or {},
                   poll_timeout=int(data.get("poll_timeout", 25)),
                   broadcast=data.get("broadcast") or None)

    def principals(self) -> dict[str, Principal]:
        return {str(uid): Principal(
                    actor_id=str(uid),
                    display_name=entry.get("name", str(uid)),
                    max_class=entry.get("max_class", "CONTROL"))
                for uid, entry in self.allowed.items()}


def write_config(token: str, path: Path | str | None = None) -> Path:
    """Create the configuration with the mode set before the content.

    `install -m 600 /dev/null` first, then write: creating the file empty and
    protected and only then filling it means the token never exists on disk
    under a permissive mode, not even for the moment between the two calls.
    """
    import os
    path = Path(path) if path else SECRETS
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    existing = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text())
        except json.JSONDecodeError:
            existing = {}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"token": token,
                   "allowed": existing.get("allowed", {}),
                   "poll_timeout": existing.get("poll_timeout", 25),
                   "broadcast": existing.get("broadcast")},
                  fh, indent=2)
    return path


def authorise(user_id: str, name: str, max_class: str = "CONTROL",
              path: Path | str | None = None) -> Path:
    """Add a principal. Separate from writing the token because they are two
    decisions: which bot, and who may drive it."""
    import os
    path = Path(path) if path else SECRETS
    data = json.loads(path.read_text())
    data.setdefault("allowed", {})[str(user_id)] = {"name": name,
                                                    "max_class": max_class}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)
    return path


def discover_senders(token: str, timeout: int = 5) -> list[dict]:
    """Who has messaged this bot? Used to find your own numeric id.

    Telegram identifies people by a number, not by @username — a username can be
    changed and reused, so the bridge authenticates on the id. Rather than
    sending you to a third-party bot to look it up, this reads the bot's own
    pending updates: message it once, run this, and it tells you who you are.
    """
    url = f"{API}/bot{token}/getUpdates"
    request = urllib.request.Request(url, data=urllib.parse.urlencode(
        {"timeout": timeout}).encode())
    try:
        with urllib.request.urlopen(request, timeout=timeout + 10) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise TelegramError(_redact(
            f"getUpdates: HTTP {exc.code}", token)) from None
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise TelegramError(_redact(f"getUpdates: {exc}", token)) from None
    if not payload.get("ok"):
        raise TelegramError(_redact(str(payload.get("description")), token))

    seen: dict[str, dict] = {}
    for update in payload.get("result") or []:
        message = update.get("message") or update.get("edited_message") or {}
        sender = message.get("from") or {}
        if not sender.get("id"):
            continue
        seen[str(sender["id"])] = {
            "id": str(sender["id"]),
            "username": sender.get("username"),
            "name": " ".join(filter(None, (sender.get("first_name"),
                                           sender.get("last_name")))),
            "chat_id": (message.get("chat") or {}).get("id"),
            "said": (message.get("text") or "")[:60],
        }
    return list(seen.values())


class TelegramBridge:
    def __init__(self, config: Config, gateway: CommandGateway, handler):
        self.config = config
        self.gateway = gateway
        self.handler = handler          # CommandIntent -> str
        self._offset = 0

    # ---- transport ------------------------------------------------------

    def _call(self, method: str, **params) -> dict:
        url = f"{API}/bot{self.config.token}/{method}"
        data = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}).encode()
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, data=data), timeout=60) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:200]
            raise TelegramError(_redact(f"{method}: HTTP {exc.code} {body}",
                                        self.config.token)) from None
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise TelegramError(_redact(f"{method}: {exc}", self.config.token)) from None
        if not payload.get("ok"):
            raise TelegramError(_redact(
                f"{method}: {payload.get('description', payload)}", self.config.token))
        return payload.get("result")

    def whoami(self) -> dict:
        bot = self._call("getMe")
        return {"id": bot.get("id"), "username": bot.get("username"),
                "name": bot.get("first_name")}

    def send(self, chat_id, text: str, thread_id: int | None = None) -> None:
        """Answer where the question was asked.

        In a forum group every topic is a separate conversation, and an answer
        that lands in the group root instead of the topic it belongs to is an
        answer nobody finds. `message_thread_id` is what keeps a reply inside
        its topic; omitting it in a plain chat is harmless, which is why it can
        simply be passed through.
        """
        # Telegram truncates at 4096; a silently cut status is a misleading one.
        extra = {"message_thread_id": thread_id} if thread_id else {}
        for chunk in (text[i:i + 3800] for i in range(0, max(len(text), 1), 3800)):
            self._call("sendMessage", chat_id=chat_id, text=chunk or "—", **extra)

    # ---- the loop -------------------------------------------------------

    @staticmethod
    def is_forwarded(message: dict) -> bool:
        """Telegram has renamed this field over the years; check all of them.

        Missing a forward marker would silently turn the strongest control in
        this bridge off, so the check errs towards treating an unfamiliar shape
        as forwarded.
        """
        for key in ("forward_origin", "forward_from", "forward_from_chat",
                    "forward_sender_name", "forward_date", "forward_signature"):
            if message.get(key):
                return True
        return False

    def poll_once(self) -> list[dict]:
        updates = self._call("getUpdates", offset=self._offset or None,
                             timeout=self.config.poll_timeout) or []
        handled = []
        for update in updates:
            self._offset = max(self._offset, update.get("update_id", 0) + 1)
            message = update.get("message") or update.get("edited_message")
            if not message:
                continue
            handled.append(self.handle(message))
        return handled

    # Telegram's own front door. A bot that answers "/start" with a refusal
    # teaches its first-time reader that it is broken; answering with what this
    # place is costs one branch.
    GREETING = (
        "DUM-E WORKSPACE\n\n"
        "Five spaces. DUM-E — the harness itself — is the first, and the "
        "other four are what a run produces evidence for.\n\n"
        "  spaces      the spaces and their channels\n"
        "  commands    everything you may do\n"
        "  open        what nobody has answered\n\n"
        "Messages here are messages. Nothing said in this chat constitutes a "
        "review, a verification or an acceptance — those are records, and they "
        "come from the gate."
    )

    def _answer(self, actor_id: str, chat_id, text: str,
                thread_id: int | None) -> str:
        """Run one command and return its reply, without sending it.

        Separate from `handle` so a reading of plain text can be executed and
        presented alongside what it was read as, rather than answered as though
        the person had typed the command.
        """
        from .command_gateway import CommandRefused
        try:
            intent = self.gateway.translate(
                actor_id=actor_id, channel=f"telegram:{chat_id}", text=text,
                forwarded=False, verified=bool(actor_id))
        except CommandRefused as exc:
            return f"refused: {exc}"
        if intent.authorization_result == "AWAITING_CONFIRMATION":
            return (f"{intent.action} is a DANGEROUS_ACTION.\n"
                    f"To go ahead, send exactly:  confirm {intent.confirmation_ref}")
        try:
            return self.handler(intent)
        except Exception as exc:
            return f"the command was authorised but failed: {type(exc).__name__}: {exc}"

    def handle(self, message: dict) -> dict:
        chat_id = (message.get("chat") or {}).get("id")
        # The topic this arrived in, when the group is a forum. Carried through
        # every reply below so an answer stays in the conversation that asked.
        thread_id = message.get("message_thread_id")
        sender = message.get("from") or {}
        # The identity comes from Telegram's own field. Nothing in the message
        # body can name a sender.
        actor_id = str(sender.get("id", ""))
        text = message.get("text") or ""
        forwarded = self.is_forwarded(message)

        if text.strip().lower() in ("/start", "start", "/help", "help", "?"):
            self.send(chat_id, self.GREETING, thread_id)
            return {"outcome": "GREETED", "actor": actor_id, "chat": chat_id}

        # Where this chat is, so the operator can make it the broadcast target
        # without hunting for a numeric id in a settings screen.
        # Addressed to somebody, or written in plain words. Both are resolved
        # before the gateway sees anything, and both are shown: a person should
        # be able to see what their sentence was taken to mean, and disagree.
        from .address import addressee, belongs_to, interpret, strip_address

        to = addressee(text)
        reading, _ = interpret(text)
        if reading:
            answer = self._answer(actor_id, chat_id, reading, thread_id)
            # One message: the reading, the numbers, and what they mean. It was
            # two, because the paraphrase took over a minute and arriving late
            # in its own message was better than delaying the answer. With the
            # thinking phase off it takes about two seconds, so the reason for
            # splitting them is gone — and a single message is what somebody
            # asking a question expects to get back.
            said = None
            try:
                from .narrate import narrate
                said = narrate(text, reading, answer or "")
            except Exception:
                said = None
            body = f"{answer or '(no output)'}"
            if said:
                body = f"{said}\n\n———\n{body}"
            self.send(chat_id, body, thread_id)
            return {"outcome": "EXECUTED", "actor": actor_id, "chat": chat_id,
                    "action": reading.split()[0]}
        if to:
            # Named an account and then said something in the vocabulary: run
            # it, and say which account it was really about when they differ.
            text = strip_address(text)

        if text.strip().lower() in ("/here", "here"):
            self.send(chat_id,
                      f"This chat is {chat_id}."
                      + (f"\nThis topic is {thread_id}." if thread_id else "")
                      + "\n\n"
                      "To have DUM-E narrate here, run on the host:\n"
                      f"  dume telegram --broadcast {chat_id}")
            return {"outcome": "GREETED", "actor": actor_id, "chat": chat_id}

        try:
            intent = self.gateway.translate(
                actor_id=actor_id, channel=f"telegram:{chat_id}", text=text,
                forwarded=forwarded, verified=bool(actor_id))
        except CommandRefused as exc:
            # A sentence that is not a command is usually a question, not an
            # attempt to run something. Refusing it with the whole vocabulary
            # printed underneath answered "ne durumda" with a list of words the
            # person was allowed to use instead. Read the state and talk about
            # it — this runs nothing and decides nothing, so it needs none of
            # the authority the refusal was protecting.
            # Only a question from an authorised person in their own words.
            # A forwarded message is text somebody else wrote, and answering it
            # would let a screenshot pasted into the chat put words in front of
            # the harness — the exact reading the forwarding guard exists to
            # refuse. An unverified actor gets the refusal too: conversing
            # reads the work's state aloud, which is not public.
            said = None
            if (not forwarded and str(actor_id) in self.config.principals()
                    and not text.strip().startswith("/")):
                try:
                    from .narrate import converse
                    said = converse(text, self._answer(actor_id, chat_id,
                                                       "status", thread_id) or "")
                except Exception:
                    said = None
            if said:
                self.send(chat_id, said, thread_id)
                return {"outcome": "ANSWERED", "actor": actor_id,
                        "chat": chat_id, "reason": "read the state and answered"}
            self.send(chat_id, f"refused: {exc}", thread_id)
            return {"outcome": "REFUSED", "actor": actor_id, "chat": chat_id,
                    "reason": str(exc)}

        if intent.authorization_result == "AWAITING_CONFIRMATION":
            self.send(chat_id,
                      f"{intent.action} is a DANGEROUS_ACTION.\n"
                      f"To go ahead, send exactly:  confirm {intent.confirmation_ref}\n"
                      "It expires in 120 seconds and only you can confirm it.",
                      thread_id)
            return {"outcome": "AWAITING_CONFIRMATION", "actor": actor_id,
                    "chat": chat_id, "action": intent.action}

        try:
            reply = self.handler(intent)
        except Exception as exc:  # a handler fault must not kill the bridge
            reply = f"the command was authorised but failed: {type(exc).__name__}: {exc}"
        # Which account the command was really about. Answered either way —
        # refusing would be pedantry — but said, so the distinction between the
        # harness and the workspace stays visible rather than becoming folklore.
        note = ""
        if to and belongs_to(intent.action) != to:
            note = (f"\n\n(You asked @{to}; {intent.action} is "
                    f"@{belongs_to(intent.action)}'s. Answered anyway.)")
        self.send(chat_id, (reply or "(no output)") + note, thread_id)
        return {"outcome": "EXECUTED", "actor": actor_id, "chat": chat_id,
                "action": intent.action}
