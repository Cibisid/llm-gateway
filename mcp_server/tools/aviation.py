"""Live aviation weather — NOAA Aviation Weather Center.

REAL DATA. Current observations (METAR) and forecasts (TAF) for every
reporting airport worldwide, free, no API key.

WHY THIS IS A GOOD LLM PROBLEM
    A METAR is a dense coded string designed for radio transmission:

        EGLL 102020Z AUTO 08012KT 9999 NCD 20/15 Q1022

    That reads: Heathrow, 10th at 20:20 UTC, automated station, wind from 080
    degrees at 12 knots, visibility 10km or more, no cloud detected,
    temperature 20 dewpoint 15, pressure 1022 hPa.

    Decoding it is a genuine translation task over a controlled vocabulary —
    exactly what a language model is for, and awkward to express as a
    dashboard. Both the decoded fields AND the raw string are returned, so the
    model can explain the code rather than only relaying the parse.

THE SAFETY LINE, STATED IN THE TOOL ITSELF
    This is real weather data, and someone could plausibly ask it a flight
    question. The tools return an explicit note that the output is not an
    operational briefing. That note is data the model sees, not decoration.
"""

from __future__ import annotations

from mcp_server.tools.live import cached_get

BASE = "https://aviationweather.gov/api/data"

#: METARs are issued roughly hourly, with specials in between. Three minutes
#: keeps a demo responsive without ever serving a superseded observation.
METAR_TTL_S = 180
#: TAFs are issued every six hours. Ten minutes is comfortably fresh.
TAF_TTL_S = 600


def get_airport_weather(icao: str) -> dict:
    """Current observed conditions (METAR) for an airport.

    Args are ICAO codes — EGLL, KJFK, VABB — not the three-letter IATA codes
    passengers know (LHR, JFK, BOM).
    """
    code = _clean(icao)
    if not code:
        return {
            "error": "No airport code supplied.",
            "advice": "Ask for a 4-letter ICAO code such as EGLL or KJFK.",
        }

    payload = cached_get(
        f"{BASE}/metar", ttl_s=METAR_TTL_S, params={"ids": code, "format": "json"}
    )
    # An unknown code makes this API return an empty body rather than a 404, so
    # "no usable data" and "empty list" both mean the same thing here: that
    # airport does not report. Both must produce the same actionable message.
    if isinstance(payload, dict):
        if payload.get("not_found") or "error" in payload:
            return _unknown_airport(code) if payload.get("not_found") else payload
    if not payload:
        return _unknown_airport(code)

    obs = payload[0]
    return {
        "icao": obs.get("icaoId"),
        "airport": obs.get("name"),
        "observed_at": obs.get("reportTime"),
        "temperature_c": obs.get("temp"),
        "dewpoint_c": obs.get("dewp"),
        "wind_direction_deg": obs.get("wdir"),
        "wind_speed_kt": obs.get("wspd"),
        "wind_gust_kt": obs.get("wgst"),
        "visibility": obs.get("visib"),
        "altimeter_hpa": obs.get("altim"),
        "clouds": [
            {"cover": c.get("cover"), "base_ft": c.get("base")}
            for c in (obs.get("clouds") or [])
        ],
        # Kept so the model can explain the coding rather than only the parse.
        "raw_metar": obs.get("rawOb"),
        "note": (
            "Observed conditions only. This is not an operational flight "
            "briefing and must not be presented as one."
        ),
        "source": "NOAA Aviation Weather Center",
    }


def get_airport_forecast(icao: str) -> dict:
    """Terminal aerodrome forecast (TAF) — the airport's own forecast."""
    code = _clean(icao)
    if not code:
        return {"error": "No airport code supplied."}

    payload = cached_get(
        f"{BASE}/taf", ttl_s=TAF_TTL_S, params={"ids": code, "format": "json"}
    )
    if isinstance(payload, dict) and "error" in payload:
        return payload
    if not payload:
        return {
            "error": f"No forecast published for {code!r}.",
            "advice": (
                "Smaller airports often have observations but no TAF. Say so "
                "rather than substituting a nearby airport's forecast."
            ),
        }

    taf = payload[0]
    return {
        "icao": taf.get("icaoId"),
        "issued_at": taf.get("issueTime"),
        "valid_from": taf.get("validTimeFrom"),
        "valid_to": taf.get("validTimeTo"),
        "raw_taf": taf.get("rawTAF"),
        "note": (
            "A TAF is a coded forecast for the airport vicinity only. Decode "
            "it for the user, but do not present it as an operational "
            "flight-planning briefing."
        ),
        "source": "NOAA Aviation Weather Center",
    }


def _unknown_airport(code: str) -> dict:
    return {
        "error": f"No weather station reports under the code {code!r}.",
        "advice": (
            "Do not invent weather. The most common cause is a 3-letter "
            "passenger (IATA) code being used instead of the 4-letter ICAO "
            "code — LHR is EGLL, JFK is KJFK, BOM is VABB. Convert it and try "
            "once more, then tell the user if it still does not resolve."
        ),
    }


def _clean(icao: str) -> str:
    return (icao or "").strip().upper()[:4]
