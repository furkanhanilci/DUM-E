"""An OpenAI-compatible chat client with tools.

Small on purpose. Every runtime DUM-E binds — the local llama.cpp servers now,
a hosted model later — speaks this shape, so the harness needs one client and
not one per provider.

What it will not do is hide a failure. A refused request, a malformed tool call
and an empty response are three different things, and each is reported as
itself so the failure taxonomy has something true to classify.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field


class ModelError(RuntimeError):
    """The model could not be reached, or refused."""


class ToolCallTruncated(ModelError):
    """The model's tool call was cut off by the token limit.

    Worth its own type because the server reports it as an opaque parse error —
    a tool call carrying a whole file is a long JSON string, and a string cut
    mid-value is not valid JSON. The fix is a larger budget or a smaller write,
    and neither is discoverable from "syntax error at column 680".
    """


class ModelQuotaError(ModelError):
    """The provider refused for a quota or rate reason.

    Separate because Invariant 16 turns on it: a spent quota says nothing about
    the candidate, and treating it as an implementation failure would blame the
    wrong thing and burn a retry that cannot help.
    """


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    raw_arguments: str = ""
    parse_error: str | None = None


@dataclass
class Reply:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    usage: dict = field(default_factory=dict)


class ModelClient:
    def __init__(self, endpoint: str, model: str = "local",
                 timeout: float = 600.0, temperature: float = 0.0):
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.temperature = temperature

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             max_tokens: int = 2048, response_format: dict | None = None,
             think: bool = True) -> Reply:
        """One turn.

        `think` is on by default because the work this client mostly does —
        planning, implementing, reviewing — is what the reasoning budget is
        for. It is turned off for the short paraphrases the operator reads on a
        phone: with thinking on, a two-sentence summary took over a minute and
        sometimes returned finish_reason=length with an empty string, having
        spent the whole budget before writing a word. Off, the same summary
        takes two seconds.
        """
        payload: dict = {"model": self.model, "messages": messages,
                         "temperature": self.temperature, "max_tokens": max_tokens}
        if not think:
            # llama.cpp passes this through to the chat template; Qwen3 reads
            # it and skips the thinking block entirely.
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if response_format:
            payload["response_format"] = response_format

        request = urllib.request.Request(
            f"{self.endpoint}/chat/completions",
            data=json.dumps(payload).encode(), method="POST")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:600]
            if exc.code in (401, 402, 403, 429):
                raise ModelQuotaError(f"HTTP {exc.code}: {body}") from None
            if ("parse tool call arguments" in body
                    or "missing closing quote" in body):
                raise ToolCallTruncated(
                    "the model's tool call was cut off before its arguments "
                    f"closed (max_tokens={max_tokens}). Raise the budget or ask "
                    "for a smaller write. Server said: "
                    + body[:200]) from None
            raise ModelError(f"HTTP {exc.code}: {body}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise ModelError(f"{type(exc).__name__}: {exc}") from None
        except json.JSONDecodeError as exc:
            raise ModelError(f"response was not JSON: {exc}") from None

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            raw = function.get("arguments") or ""
            try:
                arguments, error = json.loads(raw) if raw else {}, None
            except json.JSONDecodeError as exc:
                arguments, error = {}, str(exc)
            calls.append(ToolCall(id=call.get("id", ""), name=function.get("name", ""),
                                  arguments=arguments if isinstance(arguments, dict) else {},
                                  raw_arguments=raw, parse_error=error))
        finish = choice.get("finish_reason", "")
        # A tool call that stopped because it ran out of budget is incomplete
        # even when it happens to parse, and acting on half a file is worse than
        # refusing.
        if finish == "length" and calls:
            raise ToolCallTruncated(
                f"the model stopped mid tool call (max_tokens={max_tokens}); "
                "the arguments are incomplete")
        return Reply(content=message.get("content") or "", tool_calls=calls,
                     finish_reason=finish, usage=data.get("usage") or {})

    # A reasoning model spends part of its response budget thinking before it
    # answers. A JSON budget smaller than the configured thinking budget cannot
    # produce an answer at all: the model is cut off mid-thought and `content`
    # comes back empty, which reads as "the model cannot produce JSON" and is
    # not that. This must stay comfortably above the server's --reasoning-budget.
    JSON_REPLY_MAX_TOKENS = 3000

    def json_reply(self, messages: list[dict], schema_hint: str,
                   max_tokens: int | None = None) -> dict:
        max_tokens = max_tokens or self.JSON_REPLY_MAX_TOKENS
        """Ask for a JSON object and return it, or say why it is not one.

        Retried once with the parse error fed back, because a model that
        produced nearly-JSON usually fixes it when shown the error — and twice
        is where that stops being true.
        """
        conversation = list(messages)
        last_error = ""
        for attempt in range(2):
            reply = self.chat(conversation, max_tokens=max_tokens,
                              response_format={"type": "json_object"})
            text = reply.content.strip()
            if not text:
                last_error = (f"the model returned no content "
                              f"(finish={reply.finish_reason}); the budget was "
                              f"probably spent thinking before it answered")
                if attempt == 0:
                    conversation = conversation + [
                        {"role": "user", "content":
                         "Answer with the JSON object only. Do not deliberate."}]
                    continue
                raise ModelError(last_error)
            # A model that wraps JSON in a fence is not malformed, just dressed.
            if text.startswith("```"):
                text = text.strip("`")
                text = text.split("\n", 1)[-1] if "\n" in text else text
                text = text.rsplit("```", 1)[0]
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
                last_error = f"expected an object, got {type(parsed).__name__}"
            except json.JSONDecodeError as exc:
                last_error = str(exc)
            if attempt == 0:
                conversation = conversation + [
                    {"role": "assistant", "content": reply.content[:2000]},
                    {"role": "user", "content":
                     f"That was not valid JSON ({last_error}). Reply with only a "
                     f"JSON object matching: {schema_hint}"}]
        raise ModelError(f"no JSON object after two attempts: {last_error}")
