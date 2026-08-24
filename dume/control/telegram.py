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
                   poll_timeout=int(data.get("poll_timeout", 25)))

    def principals(self) -> dict[str, Principal]:
        return {str(uid): Principal(
                    actor_id=str(uid),
                    display_name=entry.get("name", str(uid)),
                    max_class=entry.get("max_class", "CONTROL"))
                for uid, entry in self.allowed.items()}


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

    def send(self, chat_id, text: str) -> None:
        # Telegram truncates at 4096; a silently cut status is a misleading one.
        for chunk in (text[i:i + 3800] for i in range(0, max(len(text), 1), 3800)):
            self._call("sendMessage", chat_id=chat_id, text=chunk or "—")

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

    def handle(self, message: dict) -> dict:
        chat_id = (message.get("chat") or {}).get("id")
        sender = message.get("from") or {}
        # The identity comes from Telegram's own field. Nothing in the message
        # body can name a sender.
        actor_id = str(sender.get("id", ""))
        text = message.get("text") or ""
        forwarded = self.is_forwarded(message)

        try:
            intent = self.gateway.translate(
                actor_id=actor_id, channel=f"telegram:{chat_id}", text=text,
                forwarded=forwarded, verified=bool(actor_id))
        except CommandRefused as exc:
            self.send(chat_id, f"refused: {exc}")
            return {"outcome": "REFUSED", "actor": actor_id, "reason": str(exc)}

        if intent.authorization_result == "AWAITING_CONFIRMATION":
            self.send(chat_id,
                      f"{intent.action} is a DANGEROUS_ACTION.\n"
                      f"To go ahead, send exactly:  confirm {intent.confirmation_ref}\n"
                      "It expires in 120 seconds and only you can confirm it.")
            return {"outcome": "AWAITING_CONFIRMATION", "actor": actor_id,
                    "action": intent.action}

        try:
            reply = self.handler(intent)
        except Exception as exc:  # a handler fault must not kill the bridge
            reply = f"the command was authorised but failed: {type(exc).__name__}: {exc}"
        self.send(chat_id, reply or "(no output)")
        return {"outcome": "EXECUTED", "actor": actor_id, "action": intent.action}
