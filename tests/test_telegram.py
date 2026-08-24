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
    bridge.send = lambda chat_id, text: sent.append((chat_id, text))
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
