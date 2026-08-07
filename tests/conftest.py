"""Shared test fixtures.

Every test in this suite runs with NO network access and NO API keys. A test
that only passes when a real key is present is not a regression test — it is a
smoke test that will fail in CI.
"""

from __future__ import annotations

import pytest

from app.providers.base import Provider, ProviderError, ProviderResult, Usage
from app.schemas import ChatCompletionRequest, ChatMessage


class FakeProvider(Provider):
    """A Provider implementation that never touches the network.

    Doubles as an executable specification of the contract: if a future change
    to base.py breaks this class, it breaks every real adapter too.
    """

    def __init__(
        self,
        name: str,
        models: tuple[str, ...],
        *,
        result: ProviderResult | None = None,
        error: ProviderError | None = None,
    ) -> None:
        self.name = name
        self.supported_models = models
        self._result = result
        self._error = error
        self.calls: list[ChatCompletionRequest] = []

    async def chat(self, request: ChatCompletionRequest) -> ProviderResult:
        self.calls.append(request)
        if self._error is not None:
            raise self._error
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
