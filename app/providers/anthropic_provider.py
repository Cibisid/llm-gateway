"""Anthropic adapter — the live, end-to-end verified provider path.

Three real translation problems live in this file. They are the concrete answer
to "what does it actually take to put one API in front of several vendors?"

1. SYSTEM PROMPT PLACEMENT.
   OpenAI models take the system prompt as a message with `role: "system"`.
   Anthropic takes it as a separate top-level `system=` argument and rejects a
   "system" role inside `messages`. So we hoist system messages out.

2. `max_tokens` IS REQUIRED.
   It is optional in OpenAI's API and mandatory in Anthropic's. Omitting it
   would 400, so the adapter supplies a default rather than letting a valid
   OpenAI-shaped request fail on a technicality.

3. SAMPLING PARAMETERS ARE REJECTED.
   Current Claude models return a 400 for `temperature`, `top_p`, or `top_k`.
   The gateway's OpenAI-compatible schema accepts `temperature` because real
   clients send it, so this adapter must deliberately DROP it. Forwarding it
   would break every request from a normal OpenAI client.
"""

from __future__ import annotations

from collections.abc import Sequence

import anthropic

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

# Anthropic requires max_tokens. This is the fallback when the client omits it.
DEFAULT_MAX_TOKENS = 4096

# Anthropic's stop reasons -> OpenAI's finish reasons. Anything unrecognised
# maps to "stop" so a new upstream value degrades rather than crashes.
_FINISH_REASON_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
}


class AnthropicProvider(Provider):
    name = "anthropic"
    supports_tools = True

    def __init__(self, api_key: str, models: tuple[str, ...]) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self.supported_models = models

    async def chat(
        self,
        request: ChatCompletionRequest,
        *,
        tools: Sequence[ToolSpec] = (),
        extra_turns: Sequence[Turn] = (),
    ) -> ProviderResult:
        system_prompt, messages = _split_system_messages(request)
        messages.extend(_render_turns(extra_turns))

        # Built explicitly rather than by spreading the request, so that adding
        # a field to the public schema can never silently forward something
        # Anthropic rejects.
        kwargs: dict = {
            "model": request.model,
            "max_tokens": request.max_tokens or DEFAULT_MAX_TOKENS,
            "messages": messages,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if tools:
            kwargs["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                }
                for t in tools
            ]

        # request.temperature is intentionally NOT forwarded — see module docstring.

        try:
            response = await self._client.messages.create(**kwargs)
        except anthropic.APIStatusError as exc:
            # 429 and 5xx are worth retrying elsewhere; a 400 means the request
            # itself is wrong and will fail identically on any provider.
            retryable = exc.status_code == 429 or exc.status_code >= 500
            raise ProviderError(self.name, str(exc), retryable=retryable) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(self.name, str(exc), retryable=True) from exc

        return ProviderResult(
            text=_extract_text(response),
            upstream_model=response.model,
            usage=Usage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            ),
            finish_reason=_FINISH_REASON_MAP.get(response.stop_reason or "", "stop"),
            tool_calls=_extract_tool_calls(response),
        )


def _render_turns(turns: Sequence[Turn]) -> list[dict]:
    """Translate provider-neutral turns into Anthropic's content-block format.

    Anthropic represents a tool call as a `tool_use` block on an ASSISTANT
    message, and its result as a `tool_result` block on a USER message. That
    second part is the counter-intuitive bit: results are user-role, not a
    dedicated "tool" role as OpenAI has. Getting it wrong produces a 400 that
    reads as if the conversation itself is malformed.
    """
    rendered: list[dict] = []
    for turn in turns:
        if turn.role == "assistant":
            blocks: list[dict] = []
            if turn.text:
                blocks.append({"type": "text", "text": turn.text})
            for call in turn.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                )
            if blocks:
                rendered.append({"role": "assistant", "content": blocks})
        else:
            # All outcomes go in ONE user message. Splitting them across several
            # messages is rejected, and trains the model out of parallel calls.
            rendered.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": outcome.tool_call_id,
                            "content": outcome.content,
                            "is_error": outcome.is_error,
                        }
                        for outcome in turn.tool_outcomes
                    ],
                }
            )
    return rendered


def _extract_tool_calls(response: anthropic.types.Message) -> tuple[ToolCall, ...]:
    return tuple(
        ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {}))
        for block in response.content
        if block.type == "tool_use"
    )


def _split_system_messages(
    request: ChatCompletionRequest,
) -> tuple[str | None, list[dict]]:
    """Hoist `role: "system"` messages into Anthropic's top-level `system=`.

    Multiple system messages are joined rather than dropped — OpenAI permits
    more than one, and losing any of them would silently change behaviour.
    """
    system_parts: list[str] = []
    messages: list[dict] = []

    for message in request.messages:
        if message.role == "system":
            system_parts.append(message.content)
        else:
            messages.append({"role": message.role, "content": message.content})

    return ("\n\n".join(system_parts) if system_parts else None, messages)


def _extract_text(response: anthropic.types.Message) -> str:
    """Flatten the content blocks to plain text.

    `response.content` is a list of typed blocks, and with thinking enabled the
    first one is often NOT the text block — so indexing `content[0].text` is a
    latent bug. Filter by type instead.
    """
    return "".join(block.text for block in response.content if block.type == "text")
