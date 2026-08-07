"""Adapter tests.

These target the three real translation problems the Anthropic adapter solves
(system-prompt hoisting, mandatory max_tokens, rejected sampling params). Each
one is a bug that would break every request from a normal OpenAI client, so
each one gets a test that fails loudly if the translation regresses.

No network: the vendor client is replaced with a recording double.
"""

from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx
import pytest

from app.providers.anthropic_provider import (
    DEFAULT_MAX_TOKENS,
    AnthropicProvider,
)
from app.providers.base import ProviderError
from app.providers.openai_provider import OpenAIProvider
from app.schemas import ChatCompletionRequest, ChatMessage


class RecordingMessages:
    """Stands in for `client.messages`, capturing the kwargs it was called with."""

    def __init__(self, response=None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.kwargs: dict = {}

    async def create(self, **kwargs):
        self.kwargs = kwargs
        if self._error is not None:
            raise self._error
        return self._response


def _anthropic_response(
    text: str = "hi there",
    *,
    model: str = "claude-haiku-4-5",
    stop_reason: str = "end_turn",
    blocks=None,
):
    return SimpleNamespace(
        content=blocks if blocks is not None else [SimpleNamespace(type="text", text=text)],
        model=model,
        usage=SimpleNamespace(input_tokens=12, output_tokens=7),
        stop_reason=stop_reason,
    )


def _make_anthropic(response=None, error: Exception | None = None):
    provider = AnthropicProvider("sk-ant-fake", ("claude-haiku-4-5",))
    recorder = RecordingMessages(response, error)
    provider._client = SimpleNamespace(messages=recorder)
    return provider, recorder


# --- Translation problem 1: system prompt placement ------------------------


async def test_system_messages_are_hoisted_out_of_the_messages_array():
    """Anthropic rejects a "system" role inside messages; it must move to `system=`."""
    provider, recorder = _make_anthropic(_anthropic_response())
    request = ChatCompletionRequest(
        model="claude-haiku-4-5",
        messages=[
            ChatMessage(role="system", content="You are terse."),
            ChatMessage(role="user", content="hello"),
        ],
    )

    await provider.chat(request)

    assert recorder.kwargs["system"] == "You are terse."
    assert recorder.kwargs["messages"] == [{"role": "user", "content": "hello"}]
    assert all(m["role"] != "system" for m in recorder.kwargs["messages"])


async def test_multiple_system_messages_are_joined_not_dropped():
    provider, recorder = _make_anthropic(_anthropic_response())
    request = ChatCompletionRequest(
        model="claude-haiku-4-5",
        messages=[
            ChatMessage(role="system", content="Rule one."),
            ChatMessage(role="system", content="Rule two."),
            ChatMessage(role="user", content="go"),
        ],
    )

    await provider.chat(request)

    assert recorder.kwargs["system"] == "Rule one.\n\nRule two."


async def test_no_system_key_is_sent_when_there_is_no_system_message():
    provider, recorder = _make_anthropic(_anthropic_response())
    request = ChatCompletionRequest(
        model="claude-haiku-4-5",
        messages=[ChatMessage(role="user", content="hello")],
    )

    await provider.chat(request)

    assert "system" not in recorder.kwargs


# --- Translation problem 2: max_tokens is mandatory ------------------------


async def test_max_tokens_is_supplied_when_the_client_omits_it():
    """Optional in OpenAI's API, required by Anthropic's — omitting it would 400."""
    provider, recorder = _make_anthropic(_anthropic_response())
    request = ChatCompletionRequest(
        model="claude-haiku-4-5",
        messages=[ChatMessage(role="user", content="hello")],
    )

    await provider.chat(request)

    assert recorder.kwargs["max_tokens"] == DEFAULT_MAX_TOKENS


async def test_client_supplied_max_tokens_wins():
    provider, recorder = _make_anthropic(_anthropic_response())
    request = ChatCompletionRequest(
        model="claude-haiku-4-5",
        messages=[ChatMessage(role="user", content="hello")],
        max_tokens=99,
    )

    await provider.chat(request)

    assert recorder.kwargs["max_tokens"] == 99


# --- Translation problem 3: sampling params are rejected upstream ----------


async def test_temperature_is_never_forwarded_to_anthropic():
    """Current Claude models 400 on `temperature`. Forwarding it breaks every
    request from a standard OpenAI client, which always sends one."""
    provider, recorder = _make_anthropic(_anthropic_response())
    request = ChatCompletionRequest(
        model="claude-haiku-4-5",
        messages=[ChatMessage(role="user", content="hello")],
        temperature=0.7,
    )

    await provider.chat(request)

    assert "temperature" not in recorder.kwargs
    assert "top_p" not in recorder.kwargs
    assert "top_k" not in recorder.kwargs


# --- Response normalisation ------------------------------------------------


async def test_usage_is_normalised_to_the_neutral_shape():
    provider, _ = _make_anthropic(_anthropic_response())
    request = ChatCompletionRequest(
        model="claude-haiku-4-5",
        messages=[ChatMessage(role="user", content="hello")],
    )

    result = await provider.chat(request)

    assert result.usage.input_tokens == 12
    assert result.usage.output_tokens == 7
    assert result.usage.total_tokens == 19


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        ("tool_use", "tool_calls"),
        ("refusal", "content_filter"),
        ("something_new_from_the_vendor", "stop"),
    ],
)
async def test_stop_reasons_map_onto_openai_finish_reasons(stop_reason, expected):
    provider, _ = _make_anthropic(_anthropic_response(stop_reason=stop_reason))
    request = ChatCompletionRequest(
        model="claude-haiku-4-5",
        messages=[ChatMessage(role="user", content="hello")],
    )

    result = await provider.chat(request)

    assert result.finish_reason == expected


async def test_text_extraction_skips_non_text_blocks():
    """With thinking enabled the first block is often not the text block, so
    indexing content[0].text is a latent bug. Filtering by type is not."""
    provider, _ = _make_anthropic(
        _anthropic_response(
            blocks=[
                SimpleNamespace(type="thinking", thinking="internal reasoning"),
                SimpleNamespace(type="text", text="the actual answer"),
            ]
        )
    )
    request = ChatCompletionRequest(
        model="claude-haiku-4-5",
        messages=[ChatMessage(role="user", content="hello")],
    )

    result = await provider.chat(request)

    assert result.text == "the actual answer"


# --- Error classification --------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "retryable"),
    [(400, False), (401, False), (404, False), (429, True), (500, True), (503, True)],
)
async def test_http_status_determines_whether_fallback_is_worth_trying(
    status_code, retryable
):
    """Phase 2's fallback keys on this flag: retrying a 400 elsewhere is futile,
    retrying a 429 is the whole point."""
    # A real httpx.Response is required: the SDK's exception reads
    # `response.request` during construction.
    error = anthropic.APIStatusError(
        "upstream said no",
        response=httpx.Response(
            status_code=status_code,
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        ),
        body=None,
    )
    provider, _ = _make_anthropic(error=error)
    request = ChatCompletionRequest(
        model="claude-haiku-4-5",
        messages=[ChatMessage(role="user", content="hello")],
    )

    with pytest.raises(ProviderError) as exc:
        await provider.chat(request)

    assert exc.value.retryable is retryable
    assert exc.value.provider == "anthropic"


async def test_vendor_exceptions_never_escape_the_adapter():
    """The router must not need to import a vendor SDK to handle failures."""
    error = anthropic.APIConnectionError(request=None)  # type: ignore[arg-type]
    provider, _ = _make_anthropic(error=error)
    request = ChatCompletionRequest(
        model="claude-haiku-4-5",
        messages=[ChatMessage(role="user", content="hello")],
    )

    with pytest.raises(ProviderError):
        await provider.chat(request)


# --- OpenAI adapter (mocked only — never run against the live API) ---------


async def test_openai_adapter_implements_the_same_contract():
    provider = OpenAIProvider("sk-fake", ("gpt-4o-mini",))
    recorder = RecordingMessages(
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="openai answer"),
                    finish_reason="stop",
                )
            ],
            model="gpt-4o-mini",
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=4),
        )
    )
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=recorder)
    )
    request = ChatCompletionRequest(
        model="gpt-4o-mini",
        messages=[ChatMessage(role="user", content="hello")],
        temperature=0.5,
    )

    result = await provider.chat(request)

    # Same normalised shape as Anthropic, from differently-named source fields.
    assert result.usage.input_tokens == 3
    assert result.usage.output_tokens == 4
    assert result.finish_reason == "stop"
    # Unlike Anthropic, OpenAI accepts temperature — so here it IS forwarded.
    assert recorder.kwargs["temperature"] == 0.5
