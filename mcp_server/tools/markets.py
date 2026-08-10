"""Live exchange rates — European Central Bank reference rates via Frankfurter.

REAL DATA. The ECB's official daily reference rates, free, no API key.
https://frankfurter.dev

WHY THESE RATES AND NOT A TRADING FEED
    ECB reference rates are published once a day, around 16:00 CET, and are
    the rates used for accounting, contracts, and reporting across the EU.
    They are NOT live market rates and are not what you would be quoted by a
    bank. That is a feature for this project: a fixed daily published figure
    with a known source is exactly the kind of number an AI answer should be
    grounded in and able to cite. A tick-by-tick feed would be stale the moment
    it was rendered.

    Every response says which date the rate is from, and states plainly that
    it is a reference rate. An answer that implies "this is what you would get
    at the bureau de change" would be wrong in a way that costs someone money.

THE FINANCIAL-ADVICE LINE
    Rates are data. Whether to convert money, when, or with whom, is advice —
    and this system does not give it. The note travels with the data so the
    model sees the boundary rather than inferring it.
"""

from __future__ import annotations

from datetime import date

from mcp_server.tools.live import cached_get

BASE = "https://api.frankfurter.dev/v1"

#: Published once a day, so an hour of caching cannot serve a superseded rate
#: while sparing a free public service a demo's worth of duplicate calls.
RATES_TTL_S = 3600
CURRENCIES_TTL_S = 86400


def get_exchange_rates(base_currency: str = "GBP", symbols: str = "") -> dict:
    """Latest ECB reference rates for one base currency.

    Args:
        base_currency: Three-letter code to price everything against, e.g. GBP.
        symbols: Optional comma-separated codes to limit the result,
            e.g. "USD,EUR,INR". Empty returns every available currency.
    """
    base = _clean(base_currency) or "GBP"
    params: dict = {"base": base}
    wanted = ",".join(_clean(s) for s in symbols.split(",") if s.strip())
    if wanted:
        params["symbols"] = wanted

    payload = cached_get(f"{BASE}/latest", ttl_s=RATES_TTL_S, params=params)
    if "error" in payload:
        return _explain(payload)

    rates = payload.get("rates") or {}
    if not rates:
        return {
            "error": f"No rates returned for base {base!r}.",
            "advice": (
                "The currency code may be unsupported. Call list_currencies "
                "to see valid codes rather than guessing."
            ),
        }

    return {
        "base": payload.get("base", base),
        "rate_date": payload.get("date"),
        "rates": rates,
        "note": (
            "ECB reference rates, published once each working day around 16:00 "
            "CET. These are accounting/reference rates, NOT live market rates "
            "and not what a bank or bureau de change would quote. Always state "
            "the rate_date."
        ),
        "not_advice": "Report the rate. Do not advise whether or when to convert.",
        "source": _provenance(base),
    }


def convert_currency(amount: float, from_currency: str, to_currency: str) -> dict:
    """Convert an amount at the latest ECB reference rate."""
    source, target = _clean(from_currency), _clean(to_currency)
    if not source or not target:
        return {"error": "Both from_currency and to_currency are required."}

    try:
        value = float(amount)
    except (TypeError, ValueError):
        return {"error": f"Amount {amount!r} is not a number."}

    if source == target:
        return {
            "amount": value,
            "from": source,
            "to": target,
            "converted": value,
            "rate": 1.0,
            "note": "Same currency; no conversion applied.",
        }

    payload = cached_get(
        f"{BASE}/latest", ttl_s=RATES_TTL_S, params={"base": source, "symbols": target}
    )
    if "error" in payload:
        return _explain(payload)

    rate = (payload.get("rates") or {}).get(target)
    if rate is None:
        return {
            "error": f"No rate available for {source}->{target}.",
            "advice": "Call list_currencies for valid codes.",
        }

    return {
        "amount": value,
        "from": source,
        "to": target,
        "rate": rate,
        "converted": round(value * rate, 4),
        "rate_date": payload.get("date"),
        "note": (
            "Converted at the ECB reference rate for rate_date. A real "
            "transaction would differ — banks and bureaux apply their own "
            "spread and fees."
        ),
        "not_advice": "This is a calculation, not financial advice.",
        "source": _provenance(source),
    }


def get_historical_rate(rate_date: str, base_currency: str, target_currency: str) -> dict:
    """The ECB reference rate on a past date. Date format YYYY-MM-DD."""
    base, target = _clean(base_currency), _clean(target_currency)
    day = (rate_date or "").strip()

    try:
        parsed = date.fromisoformat(day)
    except ValueError:
        return {
            "error": f"Date {day!r} is not in YYYY-MM-DD format.",
            "advice": "Ask the user for the date in that format.",
        }
    if parsed > date.today():
        return {
            "error": f"{day} is in the future.",
            "advice": (
                "Exchange rates cannot be known in advance. Say so and do not "
                "extrapolate a forecast."
            ),
        }

    payload = cached_get(
        f"{BASE}/{day}", ttl_s=CURRENCIES_TTL_S,
        params={"base": base, "symbols": target},
    )
    if "error" in payload:
        return _explain(payload)

    rate = (payload.get("rates") or {}).get(target)
    if rate is None:
        return {"error": f"No rate for {base}->{target} on {day}."}

    return {
        "requested_date": day,
        # The ECB does not publish at weekends or on holidays, so the API
        # returns the most recent working day. Surfacing both dates stops the
        # model reporting a Saturday rate that does not exist.
        "rate_date": payload.get("date"),
        "base": payload.get("base", base),
        "target": target,
        "rate": rate,
        "note": (
            "The ECB publishes only on working days. If rate_date differs from "
            "requested_date, the requested day was a weekend or holiday and "
            "this is the preceding working day's rate — say so."
        ),
        "source": _provenance(base),
    }


def list_currencies() -> dict:
    """Every currency code the rate service supports."""
    payload = cached_get(f"{BASE}/currencies", ttl_s=CURRENCIES_TTL_S)
    if "error" in payload:
        return _explain(payload)
    return {"count": len(payload), "currencies": payload}


def _provenance(base: str) -> str:
    """Describe where a rate actually came from, accurately.

    ADDED AFTER THE EVAL JUDGE CAUGHT A REAL INACCURACY. Every response used to
    claim "European Central Bank" regardless of base currency. The ECB publishes
    EURO reference rates — a GBP/USD figure is a CROSS-RATE derived by dividing
    two of them, not something the ECB publishes. The distinction matters: the
    derived rate carries the rounding of both underlying rates, and calling it
    an ECB rate overstates its authority.

    Worth noting the judge flagged this while grading a different criterion —
    an argument for LLM-as-judge alongside decidable checks rather than instead
    of them.
    """
    if base == "EUR":
        return "European Central Bank euro reference rates, via frankfurter.dev"
    return (
        f"Cross-rate derived from European Central Bank euro reference rates "
        f"(EUR/{base} and EUR/target), via frankfurter.dev. The ECB publishes "
        f"euro rates; a {base}-based rate is computed from them."
    )


def _explain(payload: dict) -> dict:
    """Turn a generic 'not found' into advice the model can act on.

    This API answers an unsupported currency code with a 404, which on its own
    reads to the model as an outage. It is not — it is a bad input, and the fix
    is to look up valid codes rather than to retry later.
    """
    if payload.get("not_found"):
        return {
            "error": "That currency code is not one the ECB publishes a rate for.",
            "advice": (
                "Call list_currencies to see the valid codes rather than "
                "guessing. Note the ECB covers major currencies only — many "
                "world currencies are simply not published."
            ),
        }
    return payload


def _clean(code: str) -> str:
    return (code or "").strip().upper()[:3]
