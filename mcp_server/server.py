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


if __name__ == "__main__":
    # Run standalone over stdio, so the server can be attached to any MCP host
    # (Claude Desktop, an IDE) and not only to this gateway.
    server.run()
