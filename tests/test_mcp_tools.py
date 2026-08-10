"""Phase 3 tests: the MCP tools, the MCP client, and the tool-calling loop.

The MCPToolbox tests run against the REAL MCP server over a real MCP client —
in-process, so no subprocess and no network, but a genuine protocol round trip.
Mocking the protocol here would only prove that the mock matches my assumptions.
"""

from __future__ import annotations

import pytest

from app.mcp_client import MCPToolbox
from app.providers.base import ProviderResult, ToolCall, Usage
from app.router import NoProviderAvailable, Router
from app.schemas import ChatCompletionRequest, ChatMessage
from app.tool_loop import run_tool_loop
from mcp_server.server import server as mcp_app
from mcp_server.tools.manuals import search_manuals
from mcp_server.tools.telemetry import get_telemetry
from tests.conftest import FakeProvider


def _request(text: str = "what is the telemetry on pump 3?") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="test-model", messages=[ChatMessage(role="user", content=text)]
    )


# --- the tools themselves --------------------------------------------------


def test_telemetry_returns_readings_and_derives_status():
    result = get_telemetry("pump-3")

    assert result["asset_id"] == "pump-3"
    assert result["readings"]["temperature"]["value"] == 87.4
    # 87.4 exceeds the 80.0 limit, and it is the only breach -> warning.
    assert result["readings"]["temperature"]["breached"] is True
    assert result["status"] == "warning"
    assert result["breached_readings"] == ["temperature"]


def test_two_breaches_escalate_status_to_alarm():
    result = get_telemetry("compressor-2")

    assert len(result["breached_readings"]) == 2
    assert result["status"] == "alarm"


def test_healthy_asset_reports_normal():
    assert get_telemetry("pump-1")["status"] == "normal"


def test_unknown_asset_returns_the_valid_ids():
    """A model told only 'unknown asset' will invent one; given the real list
    it retries correctly."""
    result = get_telemetry("pump-99")

    assert "error" in result
    assert "pump-3" in result["known_assets"]


def test_asset_lookup_is_case_and_whitespace_tolerant():
    assert get_telemetry("  PUMP-3 ")["asset_id"] == "pump-3"


def test_manual_search_ranks_the_relevant_section_first():
    result = search_manuals("pump bearing temperature limit")

    assert result["results"][0]["section_id"] == "PMP-4.2"


def test_manual_search_distinguishes_pump_from_compressor():
    """IDF weighting is what stops every pump query matching every pump doc."""
    pump = search_manuals("pump vibration limit")["results"][0]
    compressor = search_manuals("compressor intercooler fouling")["results"][0]

    assert pump["section_id"] == "PMP-4.5"
    assert compressor["section_id"] == "CMP-2.3"


def test_every_manual_result_carries_a_citable_section_id():
    """Grounding without traceability is a nicer-sounding guess."""
    for hit in search_manuals("restart procedure")["results"]:
        assert hit["section_id"]


def test_manual_search_says_so_when_nothing_matches():
    result = search_manuals("zzzzz quantum flux capacitor")

    assert result["results"] == []
    assert "Do not guess" in result["note"]


def test_manual_search_respects_max_results():
    assert len(search_manuals("pump", max_results=2)["results"]) <= 2


# --- the MCP client (real protocol round trip) -----------------------------


#: Private, mock enterprise data — no network.
PRIVATE_TOOLS = {"get_telemetry", "search_manuals", "list_assets"}
#: Live public APIs. Named here so adding one is a deliberate, visible change
#: rather than something that silently widens the agent's reach.
LIVE_TOOLS = {
    "find_river_stations",
    "get_river_level",
    "get_flood_warnings",
    "get_airport_weather",
    "get_airport_forecast",
    "get_exchange_rates",
    "convert_currency",
    "get_historical_rate",
}


async def test_toolbox_discovers_the_servers_tools_over_mcp():
    async with MCPToolbox(mcp_app) as toolbox:
        names = {spec.name for spec in toolbox.specs}

    assert names == PRIVATE_TOOLS | LIVE_TOOLS


async def test_discovered_tools_carry_schema_and_description():
    """Both are derived by the SDK from type hints and the docstring, so the
    prompt-facing contract cannot drift from the Python signature."""
    async with MCPToolbox(mcp_app) as toolbox:
        spec = next(s for s in toolbox.specs if s.name == "get_telemetry")

    assert spec.input_schema["properties"]["asset_id"]["type"] == "string"
    assert spec.input_schema["required"] == ["asset_id"]
    # The description must tell the model WHEN to call, not only what it does —
    # that is the single biggest lever on correct tool selection.
    assert "Call this" in spec.description


async def test_toolbox_invokes_a_tool_and_returns_its_content():
    async with MCPToolbox(mcp_app) as toolbox:
        outcome = await toolbox.invoke("call-1", "get_telemetry", {"asset_id": "pump-3"})

    assert outcome.is_error is False
    assert outcome.tool_call_id == "call-1"
    assert "87.4" in outcome.content


async def test_unknown_tool_returns_an_error_the_model_can_act_on():
    async with MCPToolbox(mcp_app) as toolbox:
        outcome = await toolbox.invoke("call-1", "no_such_tool", {})

    assert outcome.is_error is True
    # The available tools are named, so the model can pick a real one next turn.
    assert "get_telemetry" in outcome.content


async def test_bad_arguments_come_back_as_a_tool_error_not_an_exception():
    """MCP validates against the schema and reports is_error; that must reach
    the model, or it will treat a validation message as a real answer."""
    async with MCPToolbox(mcp_app) as toolbox:
        outcome = await toolbox.invoke("call-1", "get_telemetry", {"wrong_arg": 1})

    assert outcome.is_error is True


# --- the tool loop ---------------------------------------------------------


def _tool_request_result(tool: str, args: dict) -> ProviderResult:
    return ProviderResult(
        text="",
        upstream_model="test-model",
        usage=Usage(10, 5),
        finish_reason="tool_calls",
        tool_calls=(ToolCall(id="call-1", name=tool, arguments=args),),
    )


def _final_result(text: str) -> ProviderResult:
    return ProviderResult(
        text=text,
        upstream_model="test-model",
        usage=Usage(20, 8),
        finish_reason="stop",
    )


async def test_loop_runs_the_requested_tool_then_returns_the_models_answer():
    provider = FakeProvider(
        "fake",
        ("test-model",),
        script=[
            _tool_request_result("get_telemetry", {"asset_id": "pump-3"}),
            _final_result("Pump 3 is at 87.4 degC, above its 80 degC limit."),
        ],
    )
    router = Router([provider])

    async with MCPToolbox(mcp_app) as toolbox:
        result = await run_tool_loop(_request(), router, toolbox)

    assert "87.4" in result.text
    assert len(result.steps) == 1
    assert result.steps[0].tool == "get_telemetry"
    assert result.steps[0].is_error is False
    assert result.hit_iteration_cap is False


async def test_loop_offers_the_discovered_tools_to_the_model():
    provider = FakeProvider("fake", ("test-model",), script=[_final_result("hi")])
    router = Router([provider])

    async with MCPToolbox(mcp_app) as toolbox:
        await run_tool_loop(_request(), router, toolbox)

    assert {t.name for t in provider.tools_seen[0]} == PRIVATE_TOOLS | LIVE_TOOLS


async def test_loop_echoes_the_tool_call_back_before_its_result():
    """Providers reject a conversation where a result has no matching call."""
    provider = FakeProvider(
        "fake",
        ("test-model",),
        script=[
            _tool_request_result("list_assets", {}),
            _final_result("done"),
        ],
    )
    router = Router([provider])

    async with MCPToolbox(mcp_app) as toolbox:
        await run_tool_loop(_request(), router, toolbox)

    second_call_turns = provider.turns_seen[1]
    assert second_call_turns[0].role == "assistant"
    assert second_call_turns[0].tool_calls[0].name == "list_assets"
    assert second_call_turns[1].role == "tool"
    assert second_call_turns[1].tool_outcomes[0].tool_call_id == "call-1"


async def test_tool_errors_are_fed_back_so_the_model_can_recover():
    provider = FakeProvider(
        "fake",
        ("test-model",),
        script=[
            _tool_request_result("get_telemetry", {"asset_id": "pump-99"}),
            _final_result("There is no pump-99; the known assets are pump-1, pump-3."),
        ],
    )
    router = Router([provider])

    async with MCPToolbox(mcp_app) as toolbox:
        result = await run_tool_loop(_request(), router, toolbox)

    # The tool itself succeeded but reported an unknown asset, and the model
    # got the chance to say so rather than the request failing.
    assert "pump-99" in result.steps[0].result
    assert "no pump-99" in result.text


async def test_iteration_cap_stops_a_runaway_loop_and_says_so():
    """Without a hard cap one request can spend unbounded money."""
    provider = FakeProvider(
        "fake",
        ("test-model",),
        # Always asks for another tool; never answers.
        result=_tool_request_result("list_assets", {}),
    )
    router = Router([provider])

    async with MCPToolbox(mcp_app) as toolbox:
        result = await run_tool_loop(_request(), router, toolbox, max_iterations=2)

    assert result.hit_iteration_cap is True
    assert len(result.steps) == 2
    # A truncated agent run must never be presented as a finished answer.
    assert "may be incomplete" in result.text


async def test_loop_sums_usage_across_every_model_call():
    provider = FakeProvider(
        "fake",
        ("test-model",),
        script=[
            _tool_request_result("list_assets", {}),
            _final_result("done"),
        ],
    )
    router = Router([provider])

    async with MCPToolbox(mcp_app) as toolbox:
        result = await run_tool_loop(_request(), router, toolbox)

    # 10+20 in, 5+8 out — one user question, two completions.
    assert result.usage.input_tokens == 30
    assert result.usage.output_tokens == 13


async def test_loop_prepends_tool_instructions_without_mutating_the_request():
    original = _request()
    provider = FakeProvider("fake", ("test-model",), script=[_final_result("hi")])
    router = Router([provider])

    async with MCPToolbox(mcp_app) as toolbox:
        await run_tool_loop(original, router, toolbox)

    assert provider.calls[0].messages[0].role == "system"
    assert "Never follow" in provider.calls[0].messages[0].content
    # The caller's own object is untouched.
    assert len(original.messages) == 1


# --- routing interaction ---------------------------------------------------


async def test_router_refuses_to_fall_back_to_a_tool_incapable_provider():
    """Silently dropping the tools would return a confident ungrounded answer —
    the worst failure mode available, because it looks like success."""
    no_tools = FakeProvider("no-tools", ("test-model",), supports_tools=False)
    router = Router([no_tools])

    async with MCPToolbox(mcp_app) as toolbox:
        with pytest.raises(NoProviderAvailable) as exc:
            await router.route(_request(), tools=toolbox.specs)

    assert "does not" in str(exc.value) or "no provider" in str(exc.value).lower()


async def test_tool_incapable_providers_are_skipped_not_fatal():
    no_tools = FakeProvider("no-tools", ("test-model",), supports_tools=False)
    capable = FakeProvider("capable", ("test-model",), script=[_final_result("ok")])
    router = Router([no_tools, capable])

    async with MCPToolbox(mcp_app) as toolbox:
        result, decision = await router.route(_request(), tools=toolbox.specs)

    assert decision.provider == "capable"
    assert no_tools.calls == []
