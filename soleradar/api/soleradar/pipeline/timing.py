"""When to buy and when to sell.

The sneaker resale curve has a well-known shape: a hype peak in the drop week, a
slump 3-8 weeks later as flippers dump inventory they cannot hold, a trough, then
a slow grind back as supply dries up. Phase alone is a prior, not an answer -- so
the prior is combined with what the price actually did.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Sequence

# (upper_bound_days_since_release_exclusive, phase, prior_expected_move_pct)
# Negative days = not yet released.
PHASES: list[tuple[float, str, float]] = [
    (-14, "pre_drop", 0.0),
    (0, "drop_imminent", 0.0),
    (7, "hype_peak", -9.0),
    (45, "flipper_dump", -14.0),
    (120, "trough", 3.0),
    (540, "recovery", 12.0),
    (10_000, "mature", 5.0),
]

PHASE_COPY: dict[str, str] = {
    "pre_drop": "Not out yet. The only cheap entry is retail.",
    "drop_imminent": "Drop window. Retail allocation is the whole game right now.",
    "hype_peak": "First week. Resale is usually at its local maximum here.",
    "flipper_dump": "Weeks 1-6. Flippers who could not hold are dumping — supply peaks, price sags.",
    "trough": "Weeks 6-17. Typically the cheapest the shoe ever gets after release.",
    "recovery": "Months 4-18. Deadstock supply thins out and price grinds back.",
    "mature": "Past 18 months. Price is driven by scarcity and nostalgia, not hype.",
}


@dataclass
class TimingCall:
    phase: str
    phase_note: str
    days_since_release: int | None
    action: str                 # buy_retail | buy_resale | wait | hold | sell_now | avoid
    headline: str
    rationale: list[str] = field(default_factory=list)
    expected_move_pct: float = 0.0
    confidence: float = 0.5
    best_window: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "phase_note": self.phase_note,
            "days_since_release": self.days_since_release,
            "action": self.action,
            "headline": self.headline,
            "rationale": self.rationale,
            "expected_move_pct": round(self.expected_move_pct, 1),
            "confidence": round(self.confidence, 2),
            "best_window": self.best_window,
        }


def phase_for(days_since: int | None) -> tuple[str, float]:
    if days_since is None:
        return "unannounced", 0.0
    for upper, name, move in PHASES:
        if days_since < upper:
            return name, move
    return "mature", 5.0


def _pct(a: float, b: float) -> float:
    return ((a - b) / b * 100.0) if b else 0.0


def _ordinal(n: int) -> str:
    # 11-13 are the exception every naive implementation gets wrong.
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def assess(
    *,
    retail: float | None,
    last_sale: float | None,
    lowest_ask: float | None,
    release_date: str | None,
    heat_score: float,
    price_series: Sequence[tuple[str, float]],
    net_payout_ratio: float = 0.88,
    today: date | None = None,
) -> TimingCall:
    today = today or datetime.now(timezone.utc).date()
    days_since: int | None = None
    if release_date:
        try:
            days_since = (today - date.fromisoformat(release_date[:10])).days
        except ValueError:
            days_since = None

    phase, prior_move = phase_for(days_since)
    note = PHASE_COPY.get(phase, "Release date unknown.")
    rationale: list[str] = [note]
    prices = [p for _, p in price_series if p]
    market = last_sale or lowest_ask

    # Where does today's price sit inside the recent range? A shoe at the bottom
    # of its own 90-day range is a different trade from the same shoe at the top.
    position = None
    if len(prices) >= 6:
        lo, hi = min(prices[-90:]), max(prices[-90:])
        if hi > lo:
            position = (prices[-1] - lo) / (hi - lo)
            rationale.append(
                f"Currently at the {_ordinal(round(position * 100))} percentile of its "
                f"{min(len(prices), 90)}-observation range (${lo:,.0f} low / ${hi:,.0f} high)."
            )

    expected = prior_move
    if position is not None:
        # Bottom of range pulls expectation up, top pulls it down -- mean reversion.
        expected += (0.5 - position) * 12.0
    # Heat is forward-looking demand; let it tilt the expectation modestly.
    expected += (heat_score - 50.0) * 0.12

    # ---- decide -----------------------------------------------------------
    action = "wait"
    headline = "No clear edge right now."
    window: str | None = None
    confidence = 0.45 + (0.2 if position is not None else 0.0) + (0.1 if market else 0.0)

    if phase in {"pre_drop", "drop_imminent"}:
        if retail and market and market > retail:
            edge = _pct(market * net_payout_ratio, retail)
            if edge > 12:
                action, headline = "buy_retail", f"Enter at retail — pre-drop market implies ~{edge:.0f}% net upside."
                rationale.append(
                    f"Pre-release asks sit at ${market:,.0f} against ${retail:,.0f} retail; after "
                    f"selling fees that is roughly {edge:.0f}% net."
                )
            else:
                action, headline = "buy_retail", "Worth a retail entry, but the margin is thin."
                rationale.append(f"Implied net margin over retail is only about {edge:.0f}%.")
        elif retail and market and market <= retail:
            action, headline = "avoid", "Pre-drop market is already at or below retail."
            rationale.append("Buying at retail here means starting underwater — let it release and reassess.")
        else:
            action, headline = "buy_retail", "Retail is the only entry until the book opens."
        window = "Retail raffles / release day"
        confidence = min(confidence + 0.1, 0.9)

    elif phase == "hype_peak":
        if heat_score >= 62:
            action, headline = "sell_now", "If you are flipping, this is the window."
            rationale.append("Week-one pricing is usually the local maximum; heat is still elevated.")
        else:
            action, headline = "wait", "Hype week, but demand is not backing the price."
            rationale.append("Heat is soft for a drop week — expect the usual post-release slide.")
        window = "Now through day 7"

    elif phase == "flipper_dump":
        action = "wait" if expected < -4 else "buy_resale"
        headline = ("Hold off — supply is still hitting the market." if action == "wait"
                    else "Reasonable entry as the dump works through.")
        rationale.append("Weeks 1-6 typically print the steepest drawdown of the whole curve.")
        window = "Weeks 6-17 after release"

    elif phase == "trough":
        action, headline = "buy_resale", "This is the structural low for most releases."
        rationale.append("Post-dump supply has cleared and deadstock stock starts thinning.")
        window = "Now"
        confidence = min(confidence + 0.08, 0.9)

    elif phase in {"recovery", "mature"}:
        # Heat and expected move can genuinely disagree here: a cooling shoe
        # sitting at the bottom of its own range is a weak hold, not a sell.
        # Deciding on heat alone produced "sell now" beside a +9% forecast, which
        # is not a recommendation, it is two models talking past each other.
        bar = 45.0 if phase == "recovery" else 55.0
        conviction = (heat_score - bar) * 0.6 + expected * 1.2
        action = "hold" if conviction >= 0 else "sell_now"
        # A shoe trading at retail is a commodity, not a grail, whatever its age.
        # Calling a $105 general release "mature grail behaviour" is the kind of
        # copy that makes a whole dashboard untrustworthy.
        commodity = bool(retail and market and market <= retail * 1.15)
        if commodity:
            action = "wait"
            headline = "No edge here — it trades around retail."
            rationale.append(
                "Widely available at or near retail, so there is no spread to capture. "
                "Buy this one to wear it."
            )
        elif action == "hold":
            headline = ("Hold — the recovery leg is usually still ahead." if phase == "recovery"
                        else "Mature and still carrying a premium — hold.")
        else:
            headline = "Demand is fading and there is no catalyst to wait for."
        if (heat_score < bar) != (expected < 0):
            rationale.append(
                f"Heat ({heat_score:.0f}) and the price-range read disagree here — "
                f"{'demand is soft but the price is near its floor' if heat_score < bar else 'demand holds up but the price is near its ceiling'}. "
                "Treat this call as low conviction."
            )
            confidence -= 0.12
        window = "Reassess at 18 months" if phase == "recovery" else None

    if retail and market and market < retail * 0.95 and phase not in {"pre_drop", "drop_imminent"}:
        rationale.append(
            f"Trading below retail (${market:,.0f} vs ${retail:,.0f}) — pay retail for this only if you want to wear it."
        )

    return TimingCall(
        phase=phase, phase_note=note, days_since_release=days_since,
        action=action, headline=headline, rationale=rationale,
        expected_move_pct=expected, confidence=min(confidence, 0.92), best_window=window,
    )
