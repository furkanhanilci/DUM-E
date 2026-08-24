"""AETHRIONIS's spaces as a Telegram forum.

A forum supergroup has topics, and a topic is a conversation with its own name
and its own unread state. That is the same shape AETHRIONIS already has, so the
mapping is one to one: one topic per channel, grouped by space in the name.

Why one bot and not one per role. Telegram does not deliver a bot's messages to
another bot — that is the API's rule, not a setting. A group of role-bots could
be read by a human and could never answer each other, so the thing it looks like
it is (a conversation between roles) is the one thing it cannot be. The roles
already talk on the relay, where they can hear each other. Telegram is where a
person watches, and one identity there is honest about that.

The topic id is recorded next to the channel it mirrors, so narration lands in
the right conversation and a reply typed in a topic is understood as being about
that channel.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

MAP = Path.home() / ".dume" / "secrets" / "telegram-topics.json"

# Name, and the AETHRIONIS channel it mirrors. The space is carried in the name
# because Telegram has no grouping above a topic — so the only place the space
# can be said is in the words a person reads.
TOPICS: list[tuple[str, str]] = [
    ("DUM-E · control", "control"),
    ("DUM-E · implementation", "implementation"),
    ("DUM-E · review", "review"),
    ("DUM-E · verification", "verification"),
    ("Research · literature", "literature"),
    ("Research · questions", "questions"),
    ("Review · science", "science"),
    ("Decisions · escalations", "escalations"),
    ("Decisions · records", "records"),
    ("Operations · runtimes", "runtimes"),
    ("Operations · incidents", "incidents"),
]

# Telegram's own palette for topic icons; chosen per space so the list reads as
# groups rather than as eleven unrelated rooms.
COLOUR = {
    "DUM-E": 0xFB6F5F,      # the mark's red
    "Research": 0x6FB9F0,
    "Review": 0xFFD67E,
    "Decisions": 0x8EEE98,
    "Operations": 0xCB86DB,
}


@dataclass
class Topics:
    """The recorded mapping between AETHRIONIS channels and forum topics."""
    chat_id: str | None = None
    by_channel: dict[str, int] = None  # channel short name -> topic id

    @classmethod
    def load(cls, path: Path | str = MAP) -> "Topics":
        path = Path(path)
        if not path.is_file():
            return cls(by_channel={})
        data = json.loads(path.read_text())
        return cls(chat_id=data.get("chat_id"),
                   by_channel={k: int(v) for k, v in
                               (data.get("by_channel") or {}).items()})

    def save(self, path: Path | str = MAP) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"chat_id": self.chat_id, "by_channel": self.by_channel},
            indent=2, sort_keys=True) + "\n")
        path.chmod(0o600)
        return path

    def channel_for(self, thread_id: int | None) -> str | None:
        """Which AETHRIONIS channel a topic stands for, if any."""
        if thread_id is None:
            return None
        for channel, topic in (self.by_channel or {}).items():
            if topic == thread_id:
                return channel
        return None


def create(bridge, chat_id: str, existing: Topics | None = None,
           path: Path | str = MAP) -> Topics:
    """Create one topic per channel, skipping the ones already recorded.

    Re-runnable: a second run against the same group adds only what is missing,
    because creating a topic that already exists would give the group two rooms
    with the same name and no way to tell which one anything is in.

    `path` is explicit and defaults to the real one. It used to be implicit,
    and the test that exercises re-runnability wrote its fake chat id and fake
    topic numbers straight over the operator's own mapping — so the bridge then
    addressed a group that does not exist and Telegram answered "chat not
    found". A test that can reach production state is a test that will.
    """
    topics = existing or Topics.load(path)
    topics.chat_id = str(chat_id)
    topics.by_channel = dict(topics.by_channel or {})
    for name, channel in TOPICS:
        if channel in topics.by_channel:
            continue
        space = name.split(" · ")[0]
        result = bridge._call("createForumTopic", chat_id=chat_id, name=name,
                              icon_color=COLOUR.get(space, 0x6FB9F0))
        topics.by_channel[channel] = int(result["message_thread_id"])
    topics.save(path)
    return topics
