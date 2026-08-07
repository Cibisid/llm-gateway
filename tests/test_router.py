"""Router tests — model resolution, error classification, observability."""

from __future__ import annotations

import pytest

from app.providers.base import ProviderError, ProviderResult, Usage
from app.router import NoProviderAvailable, Router
from app.schemas import ChatCompletionRequest, ChatMessage
from tests.conftest import FakeProvider


async def test_routes_to_the_provider_that_supports_the_model(simple_request):
    wrong = FakeProvider("wrong", ("other-model",))
    right = FakeProvider("right", ("test-model",))
    router = Router([wrong, right])

    result, decision = await router.route(simple_request)

    assert decision.provider == "right"
    assert result.text == "fake answer"
    # The non-matching provider must never be called at all.
    assert wrong.calls == []


async def test_unknown_model_raises_before_any_provider_call(fake_provider):
    router = Router([fake_provider])
    request = ChatCompletionRequest(
        model="does-not-exist",
        messages=[ChatMessage(role="user", content="hi")],
    )

    with pytest.raises(NoProviderAvailable) as exc:
        await router.route(request)

    # The error must name the models that ARE available, or an operator has to
    # go read the source to find out what they typed wrong.
    assert "test-model" in str(exc.value)
    assert fake_provider.calls == []


async def test_no_providers_configured_produces_an_actionable_message():
    router = Router([])
    request = ChatCompletionRequest(
        model="anything", messages=[ChatMessage(role="user", content="hi")]
    )

    with pytest.raises(NoProviderAvailable) as exc:
        await router.route(request)

    assert "no API keys configured" in str(exc.value)


async def test_provider_errors_propagate_unchanged(simple_request):
    """Phase 1 has no fallback; the failure must surface, not be swallowed."""
    failing = FakeProvider(
        "failing",
        ("test-model",),
        error=ProviderError("failing", "upstream exploded", retryable=True),
    )
    router = Router([failing])

    with pytest.raises(ProviderError) as exc:
        await router.route(simple_request)

    assert exc.value.retryable is True


async def test_routing_decision_reports_measurable_latency(simple_request):
    router = Router([FakeProvider("fake", ("test-model",))])

    _, decision = await router.route(simple_request)

    assert decision.latency_ms >= 0


async def test_available_models_deduplicates_across_providers():
    a = FakeProvider("a", ("shared", "only-a"))
    b = FakeProvider("b", ("shared", "only-b"))

    assert Router([a, b]).available_models() == ["only-a", "only-b", "shared"]


async def test_select_candidates_returns_a_list_for_phase_2_fallback():
    """The list shape is load-bearing: Phase 2's fallback depends on it."""
    a = FakeProvider("a", ("shared",))
    b = FakeProvider("b", ("shared",))

    candidates = Router([a, b]).select_candidates("shared")

    assert [p.name for p in candidates] == ["a", "b"]


async def test_usage_totals_are_derived_not_reported():
    usage = Usage(input_tokens=100, output_tokens=25)
    assert usage.total_tokens == 125


async def test_provider_result_is_immutable():
    """Usage is a record of what happened; nothing may rewrite it."""
    result = ProviderResult(
        text="x",
        upstream_model="m",
        usage=Usage(1, 2),
        finish_reason="stop",
    )
    with pytest.raises(Exception):
        result.text = "tampered"  # type: ignore[misc]
