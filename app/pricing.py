"""Per-model pricing, and the cost calculation that depends on it.

A DELIBERATE GAP: this table contains only prices we have actually verified,
each carrying its provenance. Unpriced models are not guessed at — they return
`None`, and callers must handle that. A confidently wrong cost figure is worse
than an admitted unknown, because nobody goes back to check a number that looks
plausible.

To add a model: verify the price against the vendor's current pricing page,
add the entry, and record the source and the date you checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.providers.base import Usage

_MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class ModelPricing:
    """USD per million tokens, plus where the numbers came from."""

    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal
    source: str


# Anthropic list prices, verified against Anthropic's published model pricing
# on 2026-08-06. Note claude-sonnet-5 carries an introductory rate through
# 2026-08-31; the standard $3.00/$15.00 is used here so cost is not
# under-reported once the promotion lapses.
_ANTHROPIC_SOURCE = "Anthropic published model pricing, checked 2026-08-06"

MODEL_PRICING: dict[str, ModelPricing] = {
    "claude-opus-5": ModelPricing(Decimal("5.00"), Decimal("25.00"), _ANTHROPIC_SOURCE),
    "claude-opus-4-8": ModelPricing(Decimal("5.00"), Decimal("25.00"), _ANTHROPIC_SOURCE),
    "claude-sonnet-5": ModelPricing(Decimal("3.00"), Decimal("15.00"), _ANTHROPIC_SOURCE),
    "claude-haiku-4-5": ModelPricing(Decimal("1.00"), Decimal("5.00"), _ANTHROPIC_SOURCE),
    # OpenAI and Azure OpenAI models are intentionally absent: their prices have
    # not been verified in this environment. They will report cost_usd = null
    # until someone adds a verified entry here. Do not populate from memory.
}


def price_for(model: str) -> ModelPricing | None:
    return MODEL_PRICING.get(model)


def compute_cost_usd(model: str, usage: Usage) -> float | None:
    """Cost of one request, or None if the model has no verified price.

    Decimal rather than float: these are money figures that get summed over
    many requests, and binary floating point accumulates error in exactly the
    place a billing dashboard makes it visible.
    """
    pricing = price_for(model)
    if pricing is None:
        return None

    cost = (
        Decimal(usage.input_tokens) / _MILLION * pricing.input_usd_per_mtok
        + Decimal(usage.output_tokens) / _MILLION * pricing.output_usd_per_mtok
    )
    # 8dp: a single cheap request can cost well under a cent, and rounding to
    # 4dp would report most Haiku calls as exactly zero.
    return float(cost.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP))


def estimated_input_cost_usd(model: str) -> float | None:
    """Input price per million tokens — the ranking key for cost-based routing.

    Ranking on input price alone is a simplification: it is knowable *before*
    the call, whereas total cost depends on how many tokens the model chooses
    to emit. Output price is the tiebreak.
    """
    pricing = price_for(model)
    return float(pricing.input_usd_per_mtok) if pricing else None
