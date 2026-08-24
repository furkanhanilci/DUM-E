"""The Telegram surface: identity, forwarding, and the token."""
import json

import pytest

from dume.control.command_gateway import CommandGateway, Principal
from dume.control.telegram import Config, TelegramBridge, TelegramError, _redact

TOKEN = "123456789:AA" + "Ff1234567890abcdefGHIJKLMNOPqrst"


@pytest.fixture
def bridge(tmp_path):
    config = Config(token=TOKEN, allowed={"42": {"name": "Owner",
                                                 "max_class": "CONTROL"}})
    gateway = CommandGateway(config.principals(), audit_path=tmp_path / "a.jsonl")
    sent = []
    bridge = TelegramBridge(config, gateway, handler=lambda i: f"ran {i.action}")
    # The third argument is the forum topic a reply belongs in. Recorded so a
    # test can assert an answer landed in the conversation that asked, rather
    # than in the group root where nobody would find it.
    bridge.send = lambda chat_id, text, thread_id=None: sent.append(
        (chat_id, text, thread_id))
    bridge.sent = sent
    return bridge


def _message(text, user_id=42, **extra):
    message = {"chat": {"id": 99}, "from": {"id": user_id}, "text": text}
    message.update(extra)
    return message


def test_an_allowlisted_sender_is_obeyed(bridge):
    assert bridge.handle(_message("status"))["outcome"] == "EXECUTED"


def test_a_group_member_who_is_not_allowlisted_is_not(bridge):
    """Adding the bot to a group must not enfranchise the group."""
    result = bridge.handle(_message("pause", user_id=777))
    assert result["outcome"] == "REFUSED"
    assert "not an authorised principal" in result["reason"]


def test_identity_comes_from_telegram_not_from_the_message(bridge):
    """Nothing a message says about who sent it is consulted."""
    result = bridge.handle(_message("from: 42\npause", user_id=777))
    assert result["outcome"] == "REFUSED"


@pytest.mark.parametrize("marker", [
    "forward_origin", "forward_from", "forward_from_chat",
    "forward_sender_name", "forward_date", "forward_signature"])
def test_every_forward_marker_is_caught(bridge, marker):
    """ACC-D032. Telegram has renamed this field over the years; missing one
    would silently switch off the strongest control in the bridge."""
    result = bridge.handle(_message("pause", **{marker: "anything"}))
    assert result["outcome"] == "REFUSED"
    assert "forwarded" in result["reason"]


def test_a_dangerous_action_asks_before_acting(bridge):
    config = bridge.config
    bridge.gateway.principals["42"] = Principal("42", "Owner",
                                                max_class="DANGEROUS_ACTION")
    result = bridge.handle(_message("kill"))
    assert result["outcome"] == "AWAITING_CONFIRMATION"
    assert "confirm" in bridge.sent[-1][1]


def test_the_token_never_appears_in_an_error():
    """The token is in the URL, so the HTTP library will put it in exceptions."""
    message = _redact(f"POST https://api.telegram.org/bot{TOKEN}/getMe failed", TOKEN)
    assert TOKEN not in message
    assert "REDACTED" in message


def test_the_token_is_not_read_from_the_repository(tmp_path):
    with pytest.raises(TelegramError, match="no Telegram configuration"):
        Config.load(tmp_path / "absent.json")


def test_a_config_without_a_token_is_refused(tmp_path):
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"allowed": {"1": {}}}))
    with pytest.raises(TelegramError, match="no token"):
        Config.load(path)


def test_a_handler_fault_does_not_kill_the_bridge(bridge):
    def explode(intent):
        raise RuntimeError("handler broke")
    bridge.handler = explode
    result = bridge.handle(_message("status"))
    assert result["outcome"] == "EXECUTED"
    assert "failed" in bridge.sent[-1][1]


def test_an_answer_stays_in_the_topic_that_asked(bridge):
    """A forum group is many conversations, and the group root is not one of
    them. An answer posted there is an answer nobody finds.

    Regression guard for the reply path: whatever topic a command arrives in,
    the reply carries the same `message_thread_id` back.
    """
    message = _message("status")
    message["message_thread_id"] = 4242
    bridge.handle(message)
    assert bridge.sent[-1][2] == 4242, "the reply left its topic"

    # A plain chat has no topic, and passing none must stay harmless.
    bridge.handle(_message("status"))
    assert bridge.sent[-1][2] is None


def test_the_forum_mapping_is_re_runnable(tmp_path):
    """Creating the topics twice must not give the group two rooms of the same
    name, with no way to tell which one anything is in."""
    from dume.control import forum

    created = []

    class FakeBridge:
        def _call(self, method, **params):
            created.append(params["name"])
            return {"message_thread_id": 100 + len(created)}

    # Every call names its own file. Without this the test wrote its fake chat
    # id over the operator's real mapping, and the bridge then addressed a
    # group that does not exist.
    path = tmp_path / "topics.json"
    first = forum.create(FakeBridge(), "-100123",
                         forum.Topics(by_channel={}), path=path)
    assert len(created) == len(forum.TOPICS)
    assert not forum.MAP.samefile(path) if forum.MAP.exists() else True

    again = forum.create(FakeBridge(), "-100123", forum.Topics.load(path),
                         path=path)
    assert len(created) == len(forum.TOPICS), "a re-run created duplicates"
    assert again.by_channel == first.by_channel


def test_a_topic_names_the_channel_it_mirrors():
    """Narration has to land in the conversation it is about, and a reply typed
    in a topic has to be understood as being about that channel."""
    from dume.control.forum import Topics

    topics = Topics(chat_id="-100123", by_channel={"control": 11, "review": 12})
    assert topics.channel_for(11) == "control"
    assert topics.channel_for(12) == "review"
    assert topics.channel_for(99) is None
    assert topics.channel_for(None) is None


def test_dume_speaks_only_for_itself():
    """DUM-E is a structure inside AETHRIONIS — the harness in the laboratory,
    not the laboratory. Its narration belongs in its own channels.

    `runtime_binding` was routed to Operations, which is where AETHRIONIS talks
    about runtimes as a shared resource. A binding made for one of DUM-E's runs
    is part of that run, and sending it there made the harness sound like the
    voice of the whole workspace.
    """
    from dume.control.announce import Announcer

    for step, channel in Announcer.STEP_CHANNEL.items():
        assert channel in Announcer.OWN_CHANNELS, (
            f"{step} narrates to {channel}, which is not DUM-E's")
