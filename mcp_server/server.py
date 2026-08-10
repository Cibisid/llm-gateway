"""The MCP server: exposes the domain tools over the Model Context Protocol.

Built on mcp 2.0.0, which implements the 2026-07-28 MCP specification.
See docs/design-decisions.md for why 2.x rather than the 1.x line.

Each tool's DOCSTRING is its description, and its TYPE HINTS are its JSON
Schema — the SDK derives both. That means the prompt-facing contract and the
Python signature cannot drift apart, which is the main way hand-written tool
schemas go wrong.

The descriptions below are written for a model, not for a developer: they say
WHEN to call the tool, not just what it does. That is the single biggest lever
on whether a model calls the right tool.
"""

from __future__ import annotations

from mcp.server import MCPServer

from mcp_server.tools import aviation as _aviation
from mcp_server.tools import markets as _markets
from mcp_server.tools import rivers as _rivers
from mcp_server.tools.manuals import search_manuals as _search_manuals
from mcp_server.tools.telemetry import get_telemetry as _get_telemetry
from mcp_server.tools.telemetry import list_asset_ids

server = MCPServer("industrial-ops")


@server.tool()
def get_telemetry(asset_id: str) -> dict:
    """Get current sensor readings and alarm status for one piece of equipment.

    Call this whenever a question concerns the live condition of a specific
    asset — its temperature, vibration, pressure, flow, or whether it is in
    alarm. Do not answer such questions from memory; the readings change.

    Args:
        asset_id: The asset identifier, e.g. "pump-3", "pump-1", "compressor-2".
    """
    return _get_telemetry(asset_id)


@server.tool()
def search_manuals(query: str, max_results: int = 3) -> dict:
    """Search the maintenance manuals for procedures, limits, and thresholds.

    Call this before stating any operating limit, threshold, or maintenance
    procedure. Every result carries a section_id — cite it in your answer so a
    human can verify the claim. If no section matches, say so rather than
    inventing a procedure.

    Args:
        query: What to look up, e.g. "pump bearing temperature limit".
        max_results: How many sections to return (1-10).
    """
    return _search_manuals(query, max_results)


@server.tool()
def list_assets() -> dict:
    """List every asset id that get_telemetry can report on.

    Call this when the user names equipment ambiguously, or when get_telemetry
    reports an unknown asset, so you can resolve the correct id instead of
    guessing.
    """
    return {"assets": list_asset_ids()}


# --- live public data ------------------------------------------------------
#
# Everything above this line is private, mock enterprise data. Everything below
# calls a real external API. They sit side by side deliberately: it is the
# combination — your own equipment data plus live public feeds — that a gateway
# is for, and it shows the tool layer is not hardcoded to one domain.


@server.tool()
def find_river_stations(place: str, limit: int = 5) -> dict:
    """Find UK river-level monitoring stations by town, river, or station name.

    Call this FIRST when someone asks about a river or flooding somewhere, to
    turn a place name into a station id. There are around 4,500 stations, so
    never guess an id.

    Args:
        place: A town, river, or station name, e.g. "Oxford" or "River Thames".
        limit: Maximum stations to return (1-20).
    """
    return _rivers.find_stations(place, limit)


@server.tool()
def get_river_level(station_id: str) -> dict:
    """Get the live water level at one monitoring station.

    Levels are in mASD — metres above a zero point unique to each station — so
    a bare number means nothing. Always compare against the typical range
    returned with the reading, and never describe the value as a depth of
    water.

    Args:
        station_id: A station id from find_river_stations, e.g. "1029TH".
    """
    return _rivers.get_river_level(station_id)


@server.tool()
def get_flood_warnings(county: str = "") -> dict:
    """Get flood warnings and alerts currently in force in England.

    Call this for any question about whether somewhere is flooding or at risk.
    An empty result means no warnings are in force — say so plainly.

    Args:
        county: Optional county to filter by. Empty returns all of England.
    """
    return _rivers.get_flood_warnings(county)


@server.tool()
def get_airport_weather(icao: str) -> dict:
    """Get current observed weather (METAR) at an airport.

    Takes a 4-letter ICAO code (EGLL, KJFK), not the 3-letter code passengers
    use (LHR, JFK). If the user gives a passenger code, convert it first and
    say which airport you used.

    Args:
        icao: 4-letter ICAO airport code.
    """
    return _aviation.get_airport_weather(icao)


@server.tool()
def get_airport_forecast(icao: str) -> dict:
    """Get the aerodrome forecast (TAF) for an airport.

    Use alongside get_airport_weather when someone asks what the weather will
    do, rather than what it is doing now. Smaller airports may have no TAF.

    Args:
        icao: 4-letter ICAO airport code.
    """
    return _aviation.get_airport_forecast(icao)


@server.tool()
def get_exchange_rates(base_currency: str = "GBP", symbols: str = "") -> dict:
    """Get today's official ECB reference exchange rates.

    These are daily published reference rates, not live market rates and not
    what a bank would quote. Always state the rate date in your answer.

    Args:
        base_currency: Currency to price against, e.g. "GBP".
        symbols: Optional comma-separated codes to limit results, e.g. "USD,EUR".
    """
    return _markets.get_exchange_rates(base_currency, symbols)


@server.tool()
def convert_currency(amount: float, from_currency: str, to_currency: str) -> dict:
    """Convert an amount between currencies at the ECB reference rate.

    Report the result as a reference calculation. A real transaction differs
    because banks apply a spread and fees. Never advise whether or when to
    convert.

    Args:
        amount: How much to convert.
        from_currency: Source currency code, e.g. "GBP".
        to_currency: Target currency code, e.g. "USD".
    """
    return _markets.convert_currency(amount, from_currency, to_currency)


@server.tool()
def get_historical_rate(rate_date: str, base_currency: str, target_currency: str) -> dict:
    """Get the ECB reference rate between two currencies on a past date.

    The ECB publishes only on working days, so a weekend date returns the
    preceding working day — say so when that happens. Future dates are refused.

    Args:
        rate_date: The date, as YYYY-MM-DD.
        base_currency: Source currency code.
        target_currency: Target currency code.
    """
    return _markets.get_historical_rate(rate_date, base_currency, target_currency)


if __name__ == "__main__":
    # Run standalone over stdio, so the server can be attached to any MCP host
    # (Claude Desktop, an IDE) and not only to this gateway.
    server.run()
