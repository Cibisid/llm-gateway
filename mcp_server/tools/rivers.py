"""Live river levels and flood warnings — Environment Agency real-time API.

REAL DATA. ~4,500 monitoring stations across England, updated every 15 minutes,
free and open, no API key. https://environment.data.gov.uk/flood-monitoring/doc/reference

WHY THIS IS A GOOD FIT FOR AN LLM RATHER THAN A DASHBOARD
    A dashboard can show one station beautifully. It cannot answer "is the
    river near Oxford unusually high?", because that needs three things joined
    up: finding the station from a place name, reading its current level, and
    comparing that to *that station's own* typical range — which differs for
    every one of the 4,500. The API publishes all three; the joining is the
    work, and it is the kind of work an LLM is actually good at.

THE MEASUREMENT DETAIL THAT MATTERS
    Levels are in mASD — metres Above Station Datum — a local zero point that
    is DIFFERENT AT EVERY STATION. So a reading of 0.056 is meaningless on its
    own: it is not "5cm of water", it is 5cm above this particular gauge's
    reference. Only the comparison against that station's typicalRangeLow/High
    carries information. The tools below always return the range alongside the
    value, so the model cannot report a bare number as if it meant something.
"""

from __future__ import annotations

from mcp_server.tools.live import cached_get

BASE = "https://environment.data.gov.uk/flood-monitoring"

#: Stations report every 15 minutes, so a 5-minute cache never serves data
#: that is meaningfully stale while still collapsing a demo's repeat questions
#: into one upstream call.
READINGS_TTL_S = 300
#: The station list changes rarely — hours is fine and keeps search snappy.
STATIONS_TTL_S = 3600
#: Flood warnings are the time-critical one. Two minutes.
WARNINGS_TTL_S = 120


def find_stations(place: str, limit: int = 5) -> dict:
    """Search monitoring stations by town, river, or station name."""
    query = (place or "").strip()
    if not query:
        return {"error": "No place supplied.", "advice": "Ask which river or town."}

    # The API filters on exactly one field at a time, so we try the three that
    # a person might plausibly have meant, in order of specificity, and stop at
    # the first that hits. A single combined search endpoint does not exist.
    attempts = (
        ("riverName", query),
        ("town", query),
        ("search", query),
    )

    for field, value in attempts:
        payload = cached_get(
            f"{BASE}/id/stations",
            ttl_s=STATIONS_TTL_S,
            params={field: value, "_limit": max(1, min(limit, 20))},
        )
        if "error" in payload:
            return payload
        items = payload.get("items") or []
        if items:
            return {
                "query": query,
                "matched_on": field,
                "count": len(items),
                "stations": [_station_summary(s) for s in items],
            }

    return {
        "query": query,
        "stations": [],
        "note": (
            f"No monitoring station matched {query!r}. Say so rather than "
            "guessing a nearby one — suggest the user try a river name or a "
            "larger nearby town."
        ),
    }


def get_river_level(station_id: str) -> dict:
    """Live readings for one station, with that station's own typical range.

    The typical range is returned with every reading on purpose: mASD values
    are relative to a per-station datum and mean nothing without it.
    """
    station_id = (station_id or "").strip()
    if not station_id:
        return {"error": "No station id supplied."}

    # This endpoint HANGS on an unknown id rather than returning 404, so a bad
    # guess costs the full timeout and the model gets told "service down" when
    # the truth is "wrong id". A shorter budget here fails fast, and the advice
    # points at the actual fix.
    payload = cached_get(
        f"{BASE}/id/stations/{station_id}", ttl_s=READINGS_TTL_S, timeout_s=8.0
    )
    if "error" in payload:
        return {
            "error": f"Could not retrieve station {station_id!r}.",
            "advice": (
                "Unknown station ids hang rather than erroring on this API, so "
                "this most likely means the id does not exist. Call "
                "find_river_stations to get a real id — do not guess one, and "
                "do not report a level you have not read."
            ),
            "underlying": payload.get("error"),
        }

    station = payload.get("items")
    if not station:
        return {
            "error": f"No station with id {station_id!r}.",
            "advice": "Use find_stations first to get a valid station id.",
        }
    if isinstance(station, list):
        station = station[0]

    stage = station.get("stageScale") or {}
    low = _num(stage.get("typicalRangeLow"))
    high = _num(stage.get("typicalRangeHigh"))
    record_high = _num((stage.get("maxOnRecord") or {}).get("value"))

    readings = []
    measures = station.get("measures") or []
    if isinstance(measures, dict):
        measures = [measures]

    for measure in measures:
        latest = measure.get("latestReading") or {}
        value = _num(latest.get("value"))
        if value is None:
            continue
        readings.append(
            {
                "parameter": measure.get("parameterName"),
                "qualifier": measure.get("qualifier"),
                "value": value,
                "unit": measure.get("unitName"),
                "recorded_at": latest.get("dateTime"),
                "typical_range_low": low,
                "typical_range_high": high,
                "above_typical_range": (
                    None if (high is None or value is None) else value > high
                ),
                "below_typical_range": (
                    None if (low is None or value is None) else value < low
                ),
            }
        )

    if not readings:
        return {
            "station_id": station_id,
            "name": station.get("label"),
            "error": "The station exists but is not currently reporting a reading.",
            "advice": "Say the station is offline. Do not estimate a level.",
        }

    return {
        "station_id": station_id,
        "name": station.get("label"),
        "river": station.get("riverName"),
        "town": station.get("town"),
        "readings": readings,
        "highest_on_record": record_high,
        "datum_note": (
            "Levels are in mASD (metres Above Station Datum), a local zero "
            "point unique to this station. A value is only meaningful compared "
            "with this station's own typical range — never present it as a "
            "depth of water."
        ),
        "source": "UK Environment Agency real-time flood-monitoring API",
    }


def get_flood_warnings(county: str = "") -> dict:
    """Flood warnings and alerts currently in force, optionally by county."""
    params: dict = {}
    if county.strip():
        params["county"] = county.strip()

    payload = cached_get(f"{BASE}/id/floods", ttl_s=WARNINGS_TTL_S, params=params)
    if "error" in payload:
        return payload

    items = payload.get("items") or []
    if isinstance(items, dict):
        items = [items]

    warnings = [
        {
            "area": (w.get("description") or "").strip(),
            "severity": w.get("severity"),
            # 1 severe / 2 warning / 3 alert / 4 no longer in force. Returned
            # so the model can rank, rather than inferring from wording.
            "severity_level": w.get("severityLevel"),
            "message": (w.get("message") or "").strip()[:400],
            "raised_at": w.get("timeRaised"),
        }
        for w in items
    ]

    return {
        "county_filter": county or "(all of England)",
        "count": len(warnings),
        "warnings": warnings,
        "note": (
            "An empty list means no warnings are in force, which is good news "
            "and should be stated plainly."
        ),
        "source": "UK Environment Agency flood warnings",
    }


def _station_summary(s: dict) -> dict:
    return {
        "station_id": s.get("notation"),
        "name": s.get("label"),
        "river": s.get("riverName"),
        "town": s.get("town"),
        "catchment": s.get("catchmentName"),
    }


def _num(value) -> float | None:
    """The API returns numbers as strings in places, and omits them in others."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
