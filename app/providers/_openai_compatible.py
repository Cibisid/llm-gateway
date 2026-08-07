"""Shared implementation for OpenAI-wire-format providers.

OpenAI direct and Azure OpenAI speak the identical wire format and differ only
in how the client is constructed and how the model is addressed. Writing the
translation twice would mean fixing every future bug twice, so it lives here
and both adapters subclass it.

STATUS: everything in this module is covered by mocked tests and has NEVER been
run against a live OpenAI or Azure endpoint — no credentials are available in
this environment. Do not describe these paths as verified.

TOOL-CALLING DIFFERENCES FROM ANTHROPIC (the interesting part):
  - Tools are wrapped in a `{"type": "function", "function": {...}}` envelope;
    Anthropic takes them flat. The JSON Schema key is `parameters`, not
    `input_schema`.
  - Arguments arrive as a JSON *string* that must be parsed. Anthropic hands
    back a parsed object. A model can emit malformed JSON here, so parsing has
    to fail into a tool error rather than an exception.
  - Tool results use a dedicated `role: "tool"` message, one per result.
    Anthropic instead puts them in `tool_result` blocks on a single user
    message. This is the sharpest example of two vendors modelling the same
    concept incompatibly — and of why the `Turn` type in base.py exists.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import openai

from app.providers.base import (
    Provider,
    ProviderError,
    ProviderResult,
    ToolCall,
    ToolSpec,
    Turn,
    Usage,
)
from app.schemas import ChatCompletionRequest

_FINISH_REASON_MAP = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_calls",
    "content_filter": "content_filter",
    "function_call": "tool_calls",
}


class OpenAICompatibleProvider(Provider):
    """Base class; subclasses set `_client`, `name`, and `supported_models`."""

    supports_tools = True
    _client: openai.AsyncOpenAI

    def _upstream_model(self, request: ChatCompletionRequest) -> str:
        """Which model id to send. Azure overrides this with its deployment."""
        return request.model

    async def chat(
        self,
        request: ChatCompletionRequest,
        *,
        tools: Sequence[ToolSpec] = (),
        extra_turns: Sequence[Turn] = (),
    ) -> ProviderResult:
        messages: list[dict] = [
            {"role": m.role, "content": m.content} for m in request.messages
        ]
        messages.extend(_render_turns(extra_turns))

        kwargs: dict = {"model": self._upstream_model(request), "messages": messages}
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]

        try:
            response = await self._client.chat.completions.create(**kwargs)
        except openai.APIStatusError as exc:
            retryable = exc.status_code == 429 or exc.status_code >= 500
            raise ProviderError(self.name, str(exc), retryable=retryable) from exc
        except openai.APIConnectionError as exc:
            raise ProviderError(self.name, str(exc), retryable=True) from exc

        choice = response.choices[0]
        usage = response.usage
        return ProviderResult(
            text=choice.message.content or "",
            upstream_model=response.model,
            usage=Usage(
                input_tokens=usage.prompt_tokens if usage else 0,
                output_tokens=usage.completion_tokens if usage else 0,
            ),
            finish_reason=_FINISH_REASON_MAP.get(choice.finish_reason or "", "stop"),
            tool_calls=_extract_tool_calls(choice),
        )


def _render_turns(turns: Sequence[Turn]) -> list[dict]:
    rendered: list[dict] = []
    for turn in turns:
        if turn.role == "assistant":
            message: dict = {"role": "assistant", "content": turn.text or None}
            if turn.tool_calls:
                message["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            # Arguments go back as a JSON string, matching the
                            # shape they arrived in.
                            "arguments": json.dumps(call.arguments),
                        },
                    }
                    for call in turn.tool_calls
                ]
            rendered.append(message)
        else:
            # One message PER result here, unlike Anthropic's single user
            # message carrying every tool_result block.
            rendered.extend(
                {
                    "role": "tool",
                    "tool_call_id": outcome.tool_call_id,
                    "content": outcome.content,
                }
                for outcome in turn.tool_outcomes
            )
    return rendered


def _extract_tool_calls(choice) -> tuple[ToolCall, ...]:
    raw_calls = getattr(choice.message, "tool_calls", None) or []
    calls: list[ToolCall] = []
    for raw in raw_calls:
        try:
            arguments = json.loads(raw.function.arguments or "{}")
        except json.JSONDecodeError:
            # A model can emit malformed JSON. Surfacing it as empty arguments
            # lets the tool layer report a usable error back to the model,
            # which it can then correct — an exception would just end the turn.
            arguments = {}
        calls.append(
            ToolCall(id=raw.id, name=raw.function.name, arguments=arguments)
        )
    return tuple(calls)
