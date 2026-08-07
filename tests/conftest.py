"""Shared test fixtures.

Every test in this suite runs with NO network access and NO API keys. A test
that only passes when a real key is present is not a regression test — it is a
smoke test that will fail in CI.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from app.providers.base import (
    Provider,
    ProviderError,
    ProviderResult,
    ToolSpec,
    Turn,
    Usage,
)
from app.schemas import ChatCompletionRequest, ChatMessage


class FakeProvider(Provider):
    """A Provider implementation that never touches the network.

    Doubles as an executable specification of the contract: if a future change
    to base.py breaks this class, it breaks every real adapter too.

    `script` drives multi-turn tool tests — each entry is the ProviderResult for
    one successive call, so a test can stage "ask for a tool, then answer".
    """

    def __init__(
        self,
        name: str,
        models: tuple[str, ...],
        *,
        result: ProviderResult | None = None,
        error: ProviderError | None = None,
        script: Sequence[ProviderResult] | None = None,
        supports_tools: bool = True,
    ) -> None:
        self.name = name
        self.supported_models = models
        self.supports_tools = supports_tools
        self._result = result
        self._error = error
        self._script = list(script or [])
        self.calls: list[ChatCompletionRequest] = []
        #: Tools offered on each call, so a test can assert the model was
        #: actually given them rather than assuming it.
        self.tools_seen: list[tuple[ToolSpec, ...]] = []
        #: Conversation turns received, for asserting the loop echoes tool
        #: calls back correctly.
        self.turns_seen: list[tuple[Turn, ...]] = []

    async def chat(
        self,
        request: ChatCompletionRequest,
        *,
        tools: Sequence[ToolSpec] = (),
        extra_turns: Sequence[Turn] = (),
    ) -> ProviderResult:
        self.calls.append(request)
        self.tools_seen.append(tuple(tools))
        self.turns_seen.append(tuple(extra_turns))
        if self._error is not None:
            raise self._error
        if self._script:
            return self._script.pop(0)
        return self._result or ProviderResult(
            text="fake answer",
            upstream_model=request.model,
            usage=Usage(input_tokens=10, output_tokens=5),
            finish_reason="stop",
        )


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider("fake", ("test-model",))


@pytest.fixture
def simple_request() -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="test-model",
        messages=[ChatMessage(role="user", content="hello")],
    )
