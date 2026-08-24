"""Telling the operator something happened, without being asked.

The bridge answers questions. This is the other direction: the harness knows
when a package changed state, when a review came back and when a gate refused,
and the operator should not have to poll a machine that already knows.

It is deliberately one-way and best-effort. A narration that can fail a run is
worse than no narration: the run is the thing that matters, and Telegram being
unreachable is not an implementation failure. Every fault is collected and
reported at the end rather than raised.

Nothing here confers authority. A message saying a gate returned MERGE_ELIGIBLE
is a message about a record, not the record — the same rule the channels are
held to, and the reason the text says so.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

API = "https://api.telegram.org/bot{token}/{method}"

# What a step outcome looks like at a glance. Reused from the orchestrator's own
# vocabulary rather than invented, so the phone and the console agree.
ICON = {"OK": "✅", "BLOCKED": "⏸️", "FAILED": "❌", "REFUSED": "⛔"}


@dataclass
class Announcer:
    """Pushes to one chat. Silent when unconfigured."""
    token: str | None = None
    chat_id: str | None = None
    faults: list[str] = field(default_factory=list)
    # Which forum topic each channel is, when the target is a forum group.
    # Narration about a review belongs in the review conversation; posted to
    # the group root it is one more line in a stream nobody can follow.
    topics: dict = field(default_factory=dict)

    @classmethod
    def from_config(cls, path: Path | str | None = None) -> "Announcer":
        """Read the bridge's own configuration. Missing is not an error — a
        deployment that has not set up Telegram is a deployment that does not
        want to be told, which is a choice rather than a fault.

        `narrator_token` gives the harness its own voice. With one bot,
        "DUM-E finished a step" and "the system answered your command" arrive
        from the same name and look like the same kind of thing. They are not:
        one is a machine reporting what it did, the other is an answer to a
        person. Two identities make the difference visible without anybody
        having to read carefully.

        Falling back to the command token is deliberate — a deployment with one
        bot still gets narrated, it just gets narrated in the same voice.
        """
        from .telegram import SECRETS, TelegramError
        try:
            data = json.loads(Path(path or SECRETS).read_text())
        except (OSError, json.JSONDecodeError, TelegramError):
            return cls()
        from .forum import Topics
        return cls(token=data.get("narrator_token") or data.get("token"),
                   chat_id=data.get("broadcast"),
                   topics=Topics.load().by_channel or {})

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def say(self, text: str, *, silent: bool = False,
            channel: str | None = None) -> bool:
        """Send one message. Returns whether it left the building.

        `channel` names an AETHRIONIS channel; when the target is a forum group
        the message lands in that channel's topic. In a plain chat the topic is
        simply absent, so the same call works either way.
        """
        if not self.enabled:
            return False
        fields = {
            "chat_id": self.chat_id,
            "text": text[:4000],
            "disable_notification": "true" if silent else "false",
            # Deliberately not Markdown: a package title or a reviewer's
            # sentence containing an underscore or an asterisk would either
            # fail to send or silently change what it says, and a narration
            # that alters the words it is reporting is worse than a plain one.
            "link_preview_options": json.dumps({"is_disabled": True}),
        }
        thread = self.topics.get(channel) if channel else None
        if thread:
            fields["message_thread_id"] = thread
        payload = urllib.parse.urlencode(fields).encode()
        try:
            request = urllib.request.Request(
                API.format(token=self.token, method="sendMessage"), data=payload)
            with urllib.request.urlopen(request, timeout=8) as response:
                return json.loads(response.read().decode()).get("ok", False)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self.faults.append(f"{type(exc).__name__}: {str(exc)[:120]}")
            return False

    # ---- the shapes a run actually produces --------------------------------

    # Which conversation a step belongs to.
    #
    # All four are DUM-E's own channels. DUM-E is a structure inside AETHRIONIS
    # — the harness in the laboratory, not the laboratory — so it speaks about
    # its own work and nothing else. A runtime binding made for one of its runs
    # is part of that run; it does not belong in Operations, which is where
    # AETHRIONIS talks about runtimes as a shared resource. Routing it there
    # made DUM-E sound like the voice of the whole workspace, which it is not.
    STEP_CHANNEL = {
        "precondition": "control", "packet": "control", "cohort": "control",
        "tech_complete": "control", "machine_gate": "control",
        "runtime_binding": "implementation", "plan": "implementation",
        "worktree": "implementation", "implement": "implementation",
        "protected_paths": "implementation", "runtime_switch": "implementation",
        "specification_compliance": "review", "code_quality": "review",
        "verification": "verification",
        "failure_classification": "control", "narration": "control",
        "collaboration": "control",
    }

    # DUM-E's channels, and the only ones it writes to. A step this does not
    # recognise goes to its control channel rather than to the workspace at
    # large: an unrecognised step is still DUM-E's step.
    OWN_CHANNELS = ("control", "implementation", "review", "verification")

    def run_started(self, wp_id: str, title: str, roles: list[str]) -> None:
        self.say(f"▶ {wp_id} — {title}\n"
                 f"{len(roles)} roles: {', '.join(roles)}\n\n"
                 "Narration only. Nothing here moves the package.",
                 channel="control")

    def step(self, wp_id: str, name: str, outcome: str, detail: str) -> None:
        # Only the turns worth a notification buzz. A run has a dozen steps and
        # a phone that buzzes for each teaches its owner to stop looking.
        loud = outcome in ("FAILED", "BLOCKED")
        channel = self.STEP_CHANNEL.get(name, "control")
        if channel not in self.OWN_CHANNELS:
            channel = "control"
        self.say(f"{ICON.get(outcome, '•')} {wp_id} · {name}\n{detail[:600]}",
                 silent=not loud, channel=channel)

    def verdict(self, wp_id: str, verdict: str, seconds: float) -> None:
        self.say(f"{'✅' if verdict == 'MERGE_ELIGIBLE' else '❌'} {wp_id}: "
                 f"{verdict} in {seconds:.0f}s\n\n"
                 "This reports the gate's record. It is not the record.",
                 channel="control")

    def needs_you(self, what: str, detail: str) -> None:
        """The one thing that should always be loud: work waiting on a human.

        Still in DUM-E's control channel, not in Decisions. DUM-E can say it is
        stuck; it cannot put an item on the workspace's decision agenda, and a
        message that arrived in Decisions would look like it had.
        """
        self.say(f"🖐 {what}\n{detail[:600]}\n\n"
                 "This is the harness reporting that it is stuck. "
                 "Reply here, or: open",
                 channel="control")
