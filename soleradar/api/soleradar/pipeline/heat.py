"""The heat engine: a transparent 0-100 demand score with per-signal attribution.

Design rule: no black boxes. Every point in the final score is traceable to one
named signal, and every signal returns a sentence a human can argue with. A score
you cannot interrogate is worthless for deciding where to put money.

Each signal normalises to 0..1 where 0.5 is "unremarkable". The score is the
weighted sum x100. Contribution is reported relative to that 0.5 midpoint, so a
signal can legitimately be a *drag* rather than merely a small positive.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

# Weights sum to 1.0. Ordered by how much each has historically moved resale.
WEIGHTS: dict[str, float] = {
    "premium": 0.24,
    "trajectory": 0.18,
    "velocity": 0.16,
    "liquidity": 0.12,
    "bid_depth": 0.10,
    "scarcity": 0.08,
    "buzz": 0.07,
    "size_curve": 0.05,
}

VERDICT_BANDS: list[tuple[float, str]] = [
    (78.0, "hyped"),
    (62.0, "heating"),
    (45.0, "stable"),
    (30.0, "cooling"),
    (0.0, "sitting"),
]

# Scarcity priors by release mechanism. These are judgement calls, stated openly
# so they can be tuned rather than hidden inside a model.
SCARCITY_PRIOR: dict[str, float] = {
    "friends_family": 0.98,
    "numbered": 0.90,
    "collab": 0.78,
    "limited": 0.62,
    "general": 0.28,
}

CORE_SIZES = (8.0, 8.5, 9.0, 9.5, 10.0, 10.5, 11.0, 11.5, 12.0)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _squash(x: float, k: float = 1.0) -> float:
    """Map (-inf, inf) -> (0, 1) with 0 -> 0.5. Logistic, so extremes saturate
    instead of letting one runaway signal dominate the whole score."""
    return 1.0 / (1.0 + math.exp(-k * x))


def _money(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"${v:,.0f}"


@dataclass
class Signal:
    key: str
    label: str
    value: float          # normalised 0..1
    weight: float
    explanation: str
    raw: Any = None
    available: bool = True

    @property
    def points(self) -> float:
        """Points this signal contributes to the 0-100 score."""
        return self.value * self.weight * 100.0

    @property
    def delta(self) -> float:
        """Points relative to a neutral (0.5) reading -- the honest 'effect'."""
        return (self.value - 0.5) * self.weight * 100.0


@dataclass
class HeatResult:
    score: float
    verdict: str
    confidence: float
    signals: list[Signal] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 1),
            "verdict": self.verdict,
            "confidence": round(self.confidence, 2),
            "summary": self.summary,
            "signals": [
                {**asdict(s), "points": round(s.points, 2), "delta": round(s.delta, 2)}
                for s in self.signals
            ],
            "drivers": [s.key for s in self.top_drivers()],
            "drags": [s.key for s in self.top_drags()],
        }

    def top_drivers(self, n: int = 3) -> list[Signal]:
        return sorted([s for s in self.signals if s.delta > 0.2], key=lambda s: -s.delta)[:n]

    def top_drags(self, n: int = 3) -> list[Signal]:
        return sorted([s for s in self.signals if s.delta < -0.2], key=lambda s: s.delta)[:n]


# --------------------------------------------------------------------------
# individual signals
# --------------------------------------------------------------------------

def signal_premium(last_sale: float | None, retail: float | None) -> Signal:
    if not last_sale or not retail or retail <= 0:
        return Signal("premium", "Resale premium", 0.5, WEIGHTS["premium"],
                      "No retail price or no sales yet, so premium is unknown.", None, False)
    ratio = last_sale / retail
    # log3 scale: 3x retail is a strong but not saturating read; 1x sits at 0.5.
    norm = _clamp(_squash(math.log(ratio, 3.0), k=2.4))
    if ratio >= 1.0:
        expl = f"Trading at {ratio:.2f}x retail ({_money(last_sale)} vs {_money(retail)} retail)."
    else:
        expl = (f"Under retail at {ratio:.2f}x ({_money(last_sale)} vs {_money(retail)} retail) "
                f"— the market is clearing below the shelf price.")
    return Signal("premium", "Resale premium", norm, WEIGHTS["premium"], expl, round(ratio, 3))


def signal_trajectory(price_series: Sequence[tuple[str, float]]) -> Signal:
    """Sign and steepness of recent price movement, scaled by the series' own noise."""
    pts = [p for _, p in price_series if p]
    if len(pts) < 4:
        return Signal("trajectory", "Price trajectory", 0.5, WEIGHTS["trajectory"],
                      "Not enough price history to read a trend.", None, False)
    window = pts[-14:] if len(pts) >= 14 else pts
    n = len(window)
    xs = list(range(n))
    mx, my = sum(xs) / n, sum(window) / n
    denom = sum((x - mx) ** 2 for x in xs) or 1e-9
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, window)) / denom
    pct_per_day = (slope / my) if my else 0.0
    vol = (sum((y - my) ** 2 for y in window) / n) ** 0.5 / (my or 1)
    # Normalise the slope by the series' own volatility: +2%/day is remarkable on a
    # steady shoe and noise on a thrashy one.
    z = pct_per_day / max(vol / max(n ** 0.5, 1), 0.002)
    norm = _clamp(_squash(z, k=0.9))
    change = (window[-1] - window[0]) / window[0] * 100 if window[0] else 0.0
    direction = "up" if change >= 0 else "down"
    expl = (f"Price is {direction} {abs(change):.1f}% over the last {n} observations "
            f"({_money(window[0])} -> {_money(window[-1])}).")
    return Signal("trajectory", "Price trajectory", norm, WEIGHTS["trajectory"], expl,
                  round(change, 2))


def signal_velocity(sales_72h: int | None, baseline: float | None) -> Signal:
    if sales_72h is None:
        return Signal("velocity", "Sales velocity", 0.5, WEIGHTS["velocity"],
                      "No sales-count feed connected.", None, False)
    base = baseline if baseline and baseline > 0 else max(float(sales_72h), 1.0)
    ratio = (sales_72h + 1) / (base + 1)
    norm = _clamp(_squash(math.log(ratio, 2.0), k=1.3))
    expl = (f"{sales_72h} sales in 72h, {ratio:.2f}x its own trailing pace "
            f"({base:.0f}).")
    if ratio < 0.8:
        expl += " Trading interest is fading."
    return Signal("velocity", "Sales velocity", norm, WEIGHTS["velocity"], expl, sales_72h)


def signal_liquidity(lowest_ask: float | None, highest_bid: float | None,
                     last_sale: float | None) -> Signal:
    ref = last_sale or lowest_ask
    if not lowest_ask or not highest_bid or not ref:
        return Signal("liquidity", "Bid-ask spread", 0.5, WEIGHTS["liquidity"],
                      "Order book not available.", None, False)
    spread = (lowest_ask - highest_bid) / ref
    # 5% spread is tight for sneakers, 35% is a dead book.
    norm = _clamp(1.0 - (spread - 0.05) / 0.30)
    expl = (f"Spread is {spread * 100:.1f}% ({_money(highest_bid)} bid / {_money(lowest_ask)} ask). "
            + ("Tight book — you can exit fast." if spread < 0.12
               else "Wide book — exiting means taking a haircut."))
    return Signal("liquidity", "Bid-ask spread", norm, WEIGHTS["liquidity"], expl,
                  round(spread, 4))


def signal_bid_depth(bid_count: int | None, ask_count: int | None) -> Signal:
    if bid_count is None or ask_count is None:
        return Signal("bid_depth", "Demand pressure", 0.5, WEIGHTS["bid_depth"],
                      "Bid/ask counts not available.", None, False)
    ratio = (bid_count + 1) / (ask_count + 1)
    norm = _clamp(_squash(math.log(ratio, 2.0), k=1.0))
    expl = f"{bid_count} live bids against {ask_count} asks ({ratio:.2f}x)."
    if ratio < 0.7:
        expl += " Sellers outnumber buyers — that is what sitting looks like."
    return Signal("bid_depth", "Demand pressure", norm, WEIGHTS["bid_depth"], expl,
                  round(ratio, 3))


def signal_scarcity(release_type: str, retailer_count: int | None) -> Signal:
    prior = SCARCITY_PRIOR.get(release_type, 0.4)
    if retailer_count:
        # Broad distribution argues against scarcity whatever the label says.
        prior = _clamp(prior - 0.035 * max(0, retailer_count - 3))
    label = release_type.replace("_", " ")
    expl = f"Released as a {label} drop"
    if retailer_count:
        expl += f" through {retailer_count} tracked retailer{'s' if retailer_count != 1 else ''}"
    expl += "."
    if prior < 0.35:
        expl += " Wide availability caps how far resale can run."
    return Signal("scarcity", "Supply / scarcity", prior, WEIGHTS["scarcity"], expl, release_type)


def signal_buzz(news_ts: Sequence[str], now: datetime | None = None) -> Signal:
    now = now or datetime.now(timezone.utc)
    if not news_ts:
        return Signal("buzz", "Coverage & buzz", 0.35, WEIGHTS["buzz"],
                      "No coverage picked up in the last two weeks.", 0)
    score = 0.0
    for raw in news_ts:
        try:
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_days = max((now - ts).total_seconds() / 86400.0, 0.0)
        if age_days > 14:
            continue
        score += math.exp(-age_days / 5.0)  # half-life ~3.5 days
    norm = _clamp(_squash(math.log((score + 0.5) / 2.0, 2.0), k=1.1))
    expl = f"{len(news_ts)} mentions in 14 days (recency-weighted index {score:.1f})."
    return Signal("buzz", "Coverage & buzz", norm, WEIGHTS["buzz"], expl, round(score, 2))


def signal_size_curve(size_asks: dict[float, float], last_sale: float | None,
                      retail: float | None = None) -> Signal:
    """How BROAD the premium is across the size run.

    The naive test -- compare average core price to average tail price -- reads
    almost every shoe as unhealthy, because tails genuinely cost more on nearly
    all of them: fewer pairs are made in 4 and 15, so fewer are listed. What
    actually separates a hot shoe from a thin one is *breadth*: on real demand,
    the ordinary sizes people wear also clear above retail. When only 4s and 15s
    carry a premium, the shoe is scarce in the tails and sitting in the middle.
    """
    if not size_asks:
        return Signal("size_curve", "Size-curve breadth", 0.5, WEIGHTS["size_curve"],
                      "Per-size asks not available.", None, False)
    core = {s: v for s, v in size_asks.items() if s in CORE_SIZES}
    if not core:
        return Signal("size_curve", "Size-curve breadth", 0.5, WEIGHTS["size_curve"],
                      "No core-size asks (8-12) to read.", None, False)

    # Benchmark: retail where we know it, otherwise the shoe's own median ask.
    if retail and retail > 0:
        bar, bar_label = retail * 1.05, "retail"
    else:
        ordered = sorted(size_asks.values())
        bar, bar_label = ordered[len(ordered) // 2], "the median ask"

    above = sum(1 for v in core.values() if v >= bar)
    breadth = above / len(core)

    tails = [v for s, v in size_asks.items() if s not in CORE_SIZES]
    core_avg = sum(core.values()) / len(core)
    tail_avg = (sum(tails) / len(tails)) if tails else core_avg
    ratio = core_avg / tail_avg if tail_avg else 1.0

    # Breadth carries the signal; the core/tail ratio is a modest tiebreaker.
    norm = _clamp(0.75 * breadth + 0.25 * _clamp(_squash(math.log(max(ratio, 0.05), 2.0), k=2.2)))
    expl = (f"{above} of {len(core)} core sizes (8-12) ask above {bar_label} "
            f"({_money(bar)}); core averages {_money(core_avg)} vs {_money(tail_avg)} in the tails.")
    if breadth < 0.4:
        expl += " Premium is concentrated in the tails — demand is thinner than the headline."
    return Signal("size_curve", "Size-curve breadth", norm, WEIGHTS["size_curve"], expl,
                  round(breadth, 3))


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------

def verdict_for(score: float) -> str:
    for threshold, name in VERDICT_BANDS:
        if score >= threshold:
            return name
    return "sitting"


def _summarise(result: HeatResult, name: str) -> str:
    drivers = result.top_drivers(2)
    drags = result.top_drags(2)
    bits: list[str] = []
    if drivers:
        bits.append("carried by " + " and ".join(d.label.lower() for d in drivers))
    if drags:
        bits.append("held back by " + " and ".join(d.label.lower() for d in drags))
    tail = "; ".join(bits) if bits else "no signal is doing much work either way"
    return f"{name} scores {result.score:.0f}/100 ({result.verdict}) — {tail}."


def compute_heat(
    *,
    name: str,
    retail: float | None,
    last_sale: float | None,
    lowest_ask: float | None,
    highest_bid: float | None,
    sales_72h: int | None,
    sales_baseline: float | None,
    bid_count: int | None,
    ask_count: int | None,
    release_type: str,
    retailer_count: int | None,
    price_series: Sequence[tuple[str, float]],
    news_ts: Sequence[str],
    size_asks: dict[float, float],
    now: datetime | None = None,
) -> HeatResult:
    signals = [
        signal_premium(last_sale, retail),
        signal_trajectory(price_series),
        signal_velocity(sales_72h, sales_baseline),
        signal_liquidity(lowest_ask, highest_bid, last_sale),
        signal_bid_depth(bid_count, ask_count),
        signal_scarcity(release_type, retailer_count),
        signal_buzz(news_ts, now=now),
        signal_size_curve(size_asks, last_sale, retail),
    ]
    score = sum(s.points for s in signals)
    # Confidence is the share of the weight backed by real observations. A score
    # built mostly on fallbacks must not present itself as firmly as a full read.
    confidence = sum(s.weight for s in signals if s.available)
    result = HeatResult(score=score, verdict=verdict_for(score),
                        confidence=confidence, signals=signals)
    result.summary = _summarise(result, name)
    return result


def days_to_release(release_date: str | None, today: date | None = None) -> int | None:
    if not release_date:
        return None
    try:
        d = date.fromisoformat(release_date[:10])
    except ValueError:
        return None
    return (d - (today or datetime.now(timezone.utc).date())).days


def pre_release_heat(
    *,
    name: str,
    release_type: str,
    retailer_count: int | None,
    news_ts: Sequence[str],
    lineage_score: float | None,
    days_out: int | None,
    now: datetime | None = None,
) -> HeatResult:
    """Before a shoe drops there is no order book, so the market signals do not
    exist. Scoring it on the same eight signals would quietly score it as 'cold'.
    Pre-release uses only what genuinely exists: scarcity, buzz, lineage, timing."""
    buzz = signal_buzz(news_ts, now=now)
    scarcity = signal_scarcity(release_type, retailer_count)
    lineage_val = _clamp(lineage_score if lineage_score is not None else 0.5)
    lineage = Signal(
        "lineage", "Silhouette track record", lineage_val, 0.30,
        ("Previous colourways of this silhouette averaged "
         f"{lineage_val * 100:.0f}/100 heat." if lineage_score is not None
         else "No prior colourways of this silhouette to learn from."),
        lineage_score, lineage_score is not None,
    )
    if days_out is None:
        imminence_val, imm_expl = 0.5, "No confirmed release date yet."
    else:
        # Attention concentrates in the last fortnight before a drop.
        imminence_val = _clamp(1.0 - (max(days_out, 0) / 60.0))
        imm_expl = (f"Drops in {days_out} days." if days_out >= 0
                    else f"Released {abs(days_out)} days ago.")
    imminence = Signal("imminence", "Time to drop", imminence_val, 0.15, imm_expl, days_out)

    buzz = Signal(buzz.key, buzz.label, buzz.value, 0.35, buzz.explanation, buzz.raw, buzz.available)
    scarcity = Signal(scarcity.key, scarcity.label, scarcity.value, 0.20,
                      scarcity.explanation, scarcity.raw, scarcity.available)

    signals = [buzz, lineage, scarcity, imminence]
    score = sum(s.points for s in signals)
    confidence = sum(s.weight for s in signals if s.available) * 0.75  # pre-drop is inherently a guess
    result = HeatResult(score=score, verdict=verdict_for(score),
                        confidence=confidence, signals=signals)
    result.summary = _summarise(result, name)
    return result
