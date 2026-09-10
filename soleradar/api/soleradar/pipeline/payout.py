"""Net payout: what you actually keep, per platform.

Gross resale price is the number everyone quotes and nobody receives. This ranks
venues by *net*, which routinely reorders them -- the highest ask is often not the
best sale once commission, payment processing and shipping come out.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from functools import lru_cache
from typing import Any

from ..config import SETTINGS


@dataclass
class PayoutLine:
    platform_id: str
    platform: str
    kind: str
    gross: float
    realisation_pct: float
    commission: float
    payment_fee: float
    flat_fee: float
    shipping: float
    net: float
    net_pct: float
    payout_days: int
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("gross", "commission", "payment_fee", "flat_fee", "shipping", "net"):
            d[k] = round(d[k], 2)
        d["net_pct"] = round(d["net_pct"], 2)
        return d


@lru_cache(maxsize=1)
def load_platforms() -> dict[str, Any]:
    path = SETTINGS.seed_dir / "platforms.json"
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _commission_pct(platform: dict[str, Any], gross: float) -> float:
    """Tiered schedules are price-dependent (eBay) or volume-dependent (StockX).
    Price tiers can be resolved here; volume tiers are the caller's choice."""
    best = platform.get("commission_pct", 0.0)
    for tier in platform.get("tiers", []):
        min_price = tier.get("min_price")
        if min_price is not None and gross >= min_price:
            best = tier["commission_pct"]
    return best


def compute(gross: float, *, overrides: dict[str, dict[str, float]] | None = None) -> list[PayoutLine]:
    """Rank every platform by net proceeds on a `gross` marketplace ask.

    Fees are only half the comparison. A fee-free local sale looks like the best
    venue on paper and is not -- it clears well under the marketplace ask, and
    ranking it first on a 0% fee would be the most misleading number on the page.
    So each venue's realisation is applied to `gross` BEFORE fees come out.
    """
    overrides = overrides or {}
    lines: list[PayoutLine] = []
    for p in load_platforms()["platforms"]:
        ov = overrides.get(p["id"], {})
        realisation = ov.get("realisation_pct", p.get("realisation_pct", 100.0))
        effective = gross * realisation / 100.0

        commission_pct = ov.get("commission_pct", _commission_pct(p, effective))
        payment_pct = ov.get("payment_pct", p.get("payment_pct", 0.0))
        flat = ov.get("flat_fee", p.get("flat_fee", 0.0))
        shipping = ov.get("shipping_cost", p.get("shipping_cost", 0.0))

        commission = effective * commission_pct / 100.0
        payment_fee = effective * payment_pct / 100.0
        net = effective - commission - payment_fee - flat - shipping
        lines.append(PayoutLine(
            platform_id=p["id"], platform=p["name"], kind=p["kind"], gross=effective,
            realisation_pct=realisation,
            commission=commission, payment_fee=payment_fee, flat_fee=flat,
            shipping=shipping, net=net,
            # Measured against the ORIGINAL ask, so venues stay comparable.
            net_pct=(net / gross * 100.0) if gross else 0.0,
            payout_days=p.get("payout_days", 0), notes=p.get("notes", ""),
        ))
    lines.sort(key=lambda l: -l.net)
    return lines


def best_net_ratio(gross: float) -> float:
    """Net-to-gross ratio of the best venue -- used by the timing model so its
    'is there margin here' test is run on money you keep, not money you quote."""
    if gross <= 0:
        return 0.88
    # Realisation is modelled per venue now, so the best net is directly usable.
    return max(l.net for l in compute(gross)) / gross


def profit_on(cost: float, gross: float) -> dict[str, Any]:
    lines = compute(gross)
    best = lines[0]
    return {
        "cost": round(cost, 2),
        "gross": round(gross, 2),
        "best_platform": best.platform,
        "best_net": round(best.net, 2),
        "profit": round(best.net - cost, 2),
        "roi_pct": round(((best.net - cost) / cost * 100.0) if cost else 0.0, 2),
        "breakdown": [l.to_dict() for l in lines],
        "fee_data_verified": load_platforms().get("verified", False),
        "fee_data_as_of": load_platforms().get("as_of"),
    }
