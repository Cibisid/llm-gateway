"""OpenAI adapter.

STATUS: implemented against the same contract as the Anthropic adapter and
covered by unit tests with a mocked client. It has NEVER been run against the
live OpenAI API, because no key is available in this environment. Do not
describe this path as verified until a real key has exercised it.

This adapter has less translation work than the Anthropic one — unsurprising,
since the gateway's public schema is modelled on OpenAI's. That asymmetry is
the point: the contract in base.py absorbs the difference so the router does
not have to care which case it is dealing with.
"""

from __future__ import annotations

import openai

from app.providers.base import Provider, ProviderError, ProviderResult, Usage
from app.schemas import ChatCompletionRequest

# OpenAI's finish reasons are already the gateway's vocabulary; the map exists
# so both adapters normalise in the same visible way rather than one of them
# relying on a coincidence.
_FINISH_REASON_MAP = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_calls",
    "content_filter": "content_filter",
    "function_call": "tool_calls",
}


class OpenAIProvider(Provider):
    name = "openai"

    def __init__(self, api_key: str, models: tuple[str, ...]) -> None:
        self._client = openai.AsyncOpenAI(api_key=api_key)
        self.supported_models = models

    async def chat(self, request: ChatCompletionRequest) -> ProviderResult:
        kwargs: dict = {
            "model": request.model,
            "messages": [
                {"role": m.role, "content": m.content} for m in request.messages
            ],
        }
        # Both are optional here, unlike Anthropic — forward only if supplied
        # so we inherit OpenAI's own defaults rather than inventing our own.
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature

        try:
            response = await self._client.chat.completions.create(**kwargs)
        except openai.APIStatusError as exc:
            retryable = exc.status_code == 429 or exc.status_code >= 500
            raise ProviderError(self.name, str(exc), retryable=retryable) from exc
        except openai.APIConnectionError as exc:
            raise ProviderError(self.name, str(exc), retryable=True) from exc

        choice = response.choices[0]

        # usage is Optional in the OpenAI response type; treat a missing usage
        # block as zeroes rather than crashing the request over accounting.
        usage = response.usage
        return ProviderResult(
            text=choice.message.content or "",
            upstream_model=response.model,
            usage=Usage(
                input_tokens=usage.prompt_tokens if usage else 0,
                output_tokens=usage.completion_tokens if usage else 0,
            ),
            finish_reason=_FINISH_REASON_MAP.get(choice.finish_reason or "", "stop"),
        )
