"""Phase 2 tests: alias expansion, ranking strategies, fallback, cost."""

from __future__ import annotations

import pytest

from app.pricing import compute_cost_usd
from app.providers.base import ProviderError, ProviderResult, Usage
from app.router import Router
from app.schemas import ChatCompletionRequest, ChatMessage
from tests.conftest import FakeProvider

ALIASES = {
    "auto": ("claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5"),
    "auto-quality": ("claude-opus-5", "claude-sonnet-5"),
}


def _request(model: str) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model, messages=[ChatMessage(role="user", content="hi")]
    )


def _anthropic_like() -> FakeProvider:
    return FakeProvider(
        "anthropic", ("claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5")
    )


# --- 1. candidate expansion ------------------------------------------------


def test_alias_expands_to_its_configured_members():
    router = Router([_anthropic_like()], aliases=ALIASES)

    candidates = router.select_candidates("auto")

    assert [c.model for c in candidates] == [
        "claude-haiku-4-5",
        "claude-sonnet-5",
        "claude-opus-5",
    ]


def test_alias_silently_skips_members_no_provider_can_serve():
    """An alias must still work when only some API keys are present."""
    only_haiku = FakeProvider("anthropic", ("claude-haiku-4-5",))
    router = Router([only_haiku], aliases=ALIASES)

    candidates = router.select_candidates("auto")

    assert [c.model for c in candidates] == ["claude-haiku-4-5"]


def test_a_concrete_model_served_by_two_providers_yields_two_candidates():
    """gpt-4o via OpenAI direct and via Azure is the real-world case."""
    direct = FakeProvider("openai", ("gpt-4o",))
    azure = FakeProvider("azure_openai", ("gpt-4o",))
    router = Router([direct, azure])

    candidates = router.select_candidates("gpt-4o")

    assert [c.provider.name for c in candidates] == ["openai", "azure_openai"]


def test_unavailable_message_lists_aliases_as_well_as_models():
    router = Router([_anthropic_like()], aliases=ALIASES)

    message = router._unavailable_message("nonsense")

    assert "auto" in message
    assert "claude-haiku-4-5" in message


# --- 2. ranking ------------------------------------------------------------


def test_cost_strategy_picks_the_cheapest_member_of_an_alias():
    router = Router([_anthropic_like()], strategy="cost", aliases=ALIASES)

    candidates = router.select_candidates("auto-quality")

    # Configured order is opus-first; cost ranking must reverse it.
    assert candidates[0].model == "claude-sonnet-5"  # $3/MTok in
    assert candidates[1].model == "claude-opus-5"  # $5/MTok in


def test_cost_strategy_sorts_unpriced_models_last_not_first():
    """An unpriced model must never look like the cheapest option — we refuse
    to guess a price, and 'unknown' is not 'free'."""
    provider = FakeProvider("mixed", ("claude-opus-5", "gpt-4o-mini"))
    router = Router(
        [provider],
        strategy="cost",
        aliases={"mix": ("gpt-4o-mini", "claude-opus-5")},
    )

    candidates = router.select_candidates("mix")

    # gpt-4o-mini has no verified price, so despite being listed first it ranks
    # behind the priced (and more expensive) Opus.
    assert candidates[0].model == "claude-opus-5"
    assert candidates[1].model == "gpt-4o-mini"


def test_order_strategy_preserves_configuration_order():
    router = Router([_anthropic_like()], strategy="order", aliases=ALIASES)

    candidates = router.select_candidates("auto-quality")

    assert candidates[0].model == "claude-opus-5"


async def test_latency_strategy_prefers_the_faster_observed_provider():
    fast = FakeProvider("fast", ("shared",))
    slow = FakeProvider("slow", ("shared",))
    router = Router([slow, fast], strategy="latency")

    # Seed observations directly — the ranking reads the tracker, not the clock.
    router.latency.record("slow", 900.0)
    router.latency.record("fast", 50.0)

    candidates = router.select_candidates("shared")

    assert [c.provider.name for c in candidates] == ["fast", "slow"]


def test_latency_strategy_tries_unmeasured_providers_first():
    """Otherwise a provider that has never been sampled never gets sampled, and
    the ranking freezes on whichever one happened to be measured first."""
    measured = FakeProvider("measured", ("shared",))
    fresh = FakeProvider("fresh", ("shared",))
    router = Router([measured, fresh], strategy="latency")
    router.latency.record("measured", 10.0)

    candidates = router.select_candidates("shared")

    assert candidates[0].provider.name == "fresh"


def test_latency_ewma_is_smoothed_not_replaced():
    router = Router([], strategy="latency")

    router.latency.record("p", 100.0)
    router.latency.record("p", 200.0)

    # 0.3*200 + 0.7*100 = 130 — one slow request must not dominate.
    assert router.latency.get("p") == pytest.approx(130.0)


# --- 3. fallback -----------------------------------------------------------


async def test_retryable_failure_falls_back_to_the_next_candidate():
    failing = FakeProvider(
        "failing",
        ("shared",),
        error=ProviderError("failing", "429 rate limited", retryable=True),
    )
    working = FakeProvider("working", ("shared",))
    router = Router([failing, working])

    result, decision = await router.route(_request("shared"))

    assert result.text == "fake answer"
    assert decision.provider == "working"
    assert decision.fallback_occurred is True
    assert len(decision.attempts) == 2
    assert decision.attempts[0].ok is False
    assert decision.attempts[1].ok is True


async def test_non_retryable_failure_does_not_fall_back():
    """A malformed request fails identically on every provider; retrying it
    just buys a second guaranteed failure."""
    failing = FakeProvider(
        "failing",
        ("shared",),
        error=ProviderError("failing", "400 bad request", retryable=False),
    )
    working = FakeProvider("working", ("shared",))
    router = Router([failing, working])

    with pytest.raises(ProviderError):
        await router.route(_request("shared"))

    assert working.calls == []


async def test_when_every_candidate_fails_the_last_error_surfaces():
    a = FakeProvider("a", ("shared",), error=ProviderError("a", "503", retryable=True))
    b = FakeProvider("b", ("shared",), error=ProviderError("b", "504", retryable=True))
    router = Router([a, b])

    with pytest.raises(ProviderError) as exc:
        await router.route(_request("shared"))

    assert "504" in str(exc.value)


async def test_failed_attempts_still_feed_the_latency_tracker():
    """A provider that is timing out is slow, and the ranking should learn it."""
    failing = FakeProvider(
        "failing", ("shared",), error=ProviderError("failing", "503", retryable=True)
    )
    working = FakeProvider("working", ("shared",))
    router = Router([failing, working], strategy="latency")

    await router.route(_request("shared"))

    assert router.latency.get("failing") is not None


async def test_alias_request_calls_the_provider_with_the_concrete_model():
    """The client asked for 'auto'; the upstream must receive a real model id."""
    provider = _anthropic_like()
    router = Router([provider], strategy="cost", aliases=ALIASES)

    _, decision = await router.route(_request("auto"))

    assert provider.calls[0].model == "claude-haiku-4-5"
    assert decision.model == "claude-haiku-4-5"


# --- 4. cost ---------------------------------------------------------------


def test_cost_is_computed_from_verified_per_million_token_prices():
    # Opus 5: $5.00/MTok in, $25.00/MTok out.
    # 1,000,000 in + 1,000,000 out = $5 + $25 = $30.
    cost = compute_cost_usd(
        "claude-opus-5", Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    )
    assert cost == pytest.approx(30.0)


def test_small_requests_do_not_round_to_zero():
    """4dp would report most Haiku calls as free, which is worse than useless."""
    cost = compute_cost_usd(
        "claude-haiku-4-5", Usage(input_tokens=100, output_tokens=50)
    )
    assert cost is not None and cost > 0


def test_unpriced_model_reports_none_rather_than_a_guess():
    assert compute_cost_usd("gpt-4o", Usage(input_tokens=100, output_tokens=50)) is None


async def test_routing_decision_carries_the_cost_of_the_model_that_ran():
    provider = FakeProvider(
        "anthropic",
        ("claude-haiku-4-5",),
        result=ProviderResult(
            text="x",
            upstream_model="claude-haiku-4-5",
            usage=Usage(input_tokens=1_000_000, output_tokens=0),
            finish_reason="stop",
        ),
    )
    router = Router([provider])

    _, decision = await router.route(_request("claude-haiku-4-5"))

    # $1.00/MTok input.
    assert decision.cost_usd == pytest.approx(1.0)


async def test_cost_is_null_when_the_served_model_has_no_verified_price():
    provider = FakeProvider("openai", ("gpt-4o",))
    router = Router([provider])

    _, decision = await router.route(_request("gpt-4o"))

    assert decision.cost_usd is None
