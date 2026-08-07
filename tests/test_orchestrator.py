"""Phase 4 tests: planning, the act/observe loop, and A2A delegation.

The delegation tests drive a REAL A2A round trip — a JSON-RPC request over HTTP
into the app's own `/a2a/specialist` endpoint — rather than mocking the protocol
they exist to exercise.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.a2a import (
    build_send_request,
    build_task_response,
    extract_task_text,
    extract_text,
)
from app.mcp_client import MCPToolbox
from app.orchestrator import DELEGATE_TOOL, Orchestrator, _parse_plan
from app.providers.base import ProviderResult, ToolCall, Usage
from app.router import Router
from app.schemas import ChatCompletionRequest, ChatMessage
from mcp_server.server import server as mcp_app
from tests.conftest import FakeProvider


def _request(text: str = "Is pump 3 overheating and why?") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="test-model", messages=[ChatMessage(role="user", content=text)]
    )


def _plan_result(json_text: str) -> ProviderResult:
    return ProviderResult(
        text=json_text,
        upstream_model="test-model",
        usage=Usage(5, 5),
        finish_reason="stop",
    )


def _tool_result(tool: str, args: dict) -> ProviderResult:
    return ProviderResult(
        text="",
        upstream_model="test-model",
        usage=Usage(10, 5),
        finish_reason="tool_calls",
        tool_calls=(ToolCall(id="c1", name=tool, arguments=args),),
    )


def _answer(text: str) -> ProviderResult:
    return ProviderResult(
        text=text, upstream_model="test-model", usage=Usage(10, 5), finish_reason="stop"
    )


GOOD_PLAN = '{"goal": "Diagnose pump 3", "steps": ["Check telemetry", "Look up limit"]}'


# --- plan parsing ----------------------------------------------------------


def test_parses_a_clean_json_plan():
    plan = _parse_plan(GOOD_PLAN)

    assert plan.goal == "Diagnose pump 3"
    assert plan.steps == ["Check telemetry", "Look up limit"]
    assert plan.malformed is False


def test_parses_a_plan_wrapped_in_a_fenced_code_block():
    """Models routinely fence JSON; failing the run over formatting would be
    failing over the wrong thing."""
    plan = _parse_plan(f"Here is the plan:\n```json\n{GOOD_PLAN}\n```")

    assert plan.steps == ["Check telemetry", "Look up limit"]
    assert plan.malformed is False


def test_parses_a_plan_with_prose_around_it():
    plan = _parse_plan(f"Sure! {GOOD_PLAN} Let me know if that works.")

    assert plan.goal == "Diagnose pump 3"


def test_unparseable_plan_is_flagged_not_fatal():
    """A bad plan is a degraded start, not a failed request — but the caller
    must be told, or they see a plan that did not actually guide the run."""
    plan = _parse_plan("I'll just have a look at the pump.")

    assert plan.malformed is True
    assert plan.steps == []
    assert plan.raw == "I'll just have a look at the pump."


def test_a_plan_with_no_steps_counts_as_malformed():
    assert _parse_plan('{"goal": "x", "steps": []}').malformed is True


# --- the orchestration loop ------------------------------------------------


async def _orchestrator(provider: FakeProvider, http_client=None, **kwargs):
    router = Router([provider])
    toolbox = await MCPToolbox(mcp_app).__aenter__()
    return (
        Orchestrator(
            router,
            toolbox,
            specialist_url="/a2a/specialist",
            http_client=http_client,
            **kwargs,
        ),
        toolbox,
    )


async def test_orchestrator_plans_before_acting():
    provider = FakeProvider(
        "fake", ("test-model",), script=[_plan_result(GOOD_PLAN), _answer("Done.")]
    )
    orch, toolbox = await _orchestrator(provider)
    try:
        result = await orch.run(_request())
    finally:
        await toolbox.__aexit__(None, None, None)

    assert result.plan.goal == "Diagnose pump 3"
    assert len(result.plan.steps) == 2
    # The planning call must come first, and must NOT be offered tools —
    # planning is deciding what to do, not doing it.
    assert provider.tools_seen[0] == ()


def test_the_plan_is_visible_before_execution_for_approval_gating():
    """The plan being inspectable is its main reason for existing."""
    plan = _parse_plan(GOOD_PLAN)
    assert plan.steps  # a human (or policy) could gate on this


async def test_the_agreed_plan_is_passed_into_the_execution_stage():
    provider = FakeProvider(
        "fake", ("test-model",), script=[_plan_result(GOOD_PLAN), _answer("Done.")]
    )
    orch, toolbox = await _orchestrator(provider)
    try:
        await orch.run(_request())
    finally:
        await toolbox.__aexit__(None, None, None)

    executor_prompt = provider.calls[1].messages[1].content
    assert "Check telemetry" in executor_prompt
    assert "Diagnose pump 3" in executor_prompt


async def test_orchestrator_runs_mcp_tools_during_execution():
    provider = FakeProvider(
        "fake",
        ("test-model",),
        script=[
            _plan_result(GOOD_PLAN),
            _tool_result("get_telemetry", {"asset_id": "pump-3"}),
            _answer("Pump 3 is at 87.4 degC."),
        ],
    )
    orch, toolbox = await _orchestrator(provider)
    try:
        result = await orch.run(_request())
    finally:
        await toolbox.__aexit__(None, None, None)

    assert result.steps[0].tool == "get_telemetry"
    assert "87.4" in result.steps[0].result
    assert result.delegated is False


async def test_delegation_is_offered_to_the_model_as_just_another_tool():
    """The model chooses to delegate the same way it chooses to look something
    up — no hardcoded branch decides it."""
    provider = FakeProvider(
        "fake", ("test-model",), script=[_plan_result(GOOD_PLAN), _answer("ok")]
    )
    orch, toolbox = await _orchestrator(provider)
    try:
        await orch.run(_request())
    finally:
        await toolbox.__aexit__(None, None, None)

    offered = {t.name for t in provider.tools_seen[1]}
    assert DELEGATE_TOOL.name in offered
    assert "get_telemetry" in offered


async def test_step_cap_stops_a_runaway_agent_and_labels_the_answer():
    provider = FakeProvider(
        "fake",
        ("test-model",),
        script=[_plan_result(GOOD_PLAN)],
        result=_tool_result("list_assets", {}),
    )
    orch, toolbox = await _orchestrator(provider, max_steps=2)
    try:
        result = await orch.run(_request())
    finally:
        await toolbox.__aexit__(None, None, None)

    assert result.hit_step_cap is True
    assert "incomplete" in result.text
    assert len(result.steps) == 2


async def test_usage_and_cost_include_the_planning_call():
    """The plan is a real model call; omitting it under-reports what the agent
    cost."""
    provider = FakeProvider(
        "fake", ("test-model",), script=[_plan_result(GOOD_PLAN), _answer("done")]
    )
    orch, toolbox = await _orchestrator(provider)
    try:
        result = await orch.run(_request())
    finally:
        await toolbox.__aexit__(None, None, None)

    # 5+10 in, 5+5 out — planner plus executor.
    assert result.usage.input_tokens == 15
    assert result.usage.output_tokens == 10


async def test_delegation_without_a_question_is_rejected_usefully():
    provider = FakeProvider(
        "fake",
        ("test-model",),
        script=[
            _plan_result(GOOD_PLAN),
            _tool_result(DELEGATE_TOOL.name, {}),
            _answer("done"),
        ],
    )
    orch, toolbox = await _orchestrator(provider)
    try:
        result = await orch.run(_request())
    finally:
        await toolbox.__aexit__(None, None, None)

    assert result.steps[0].is_error is True
    assert "readings" in result.steps[0].result


async def test_unreachable_specialist_degrades_instead_of_500ing():
    """A failed consult should not lose an answer the agent could partly give."""
    provider = FakeProvider(
        "fake",
        ("test-model",),
        script=[
            _plan_result(GOOD_PLAN),
            _tool_result(DELEGATE_TOOL.name, {"question": "why hot?"}),
            _answer("Consult failed; based on readings alone, likely bearing wear."),
        ],
    )
    # http_client=None simulates the specialist being unreachable.
    orch, toolbox = await _orchestrator(provider, http_client=None)
    try:
        result = await orch.run(_request())
    finally:
        await toolbox.__aexit__(None, None, None)

    assert result.steps[0].is_error is True
    assert "not reachable" in result.steps[0].result
    # The run still produced an answer.
    assert "bearing wear" in result.text


# --- the A2A wire format ---------------------------------------------------


def test_agent_card_advertises_skills_and_is_honest_about_capabilities():
    from agents.specialist_agent import agent_card

    card = agent_card("http://example.test").to_dict()

    assert card["protocolVersion"]
    assert {s["id"] for s in card["skills"]} == {
        "failure-mode-analysis",
        "next-check-recommendation",
    }
    # Streaming is genuinely not implemented, so the card must not claim it.
    assert card["capabilities"]["streaming"] is False


def test_send_request_is_valid_json_rpc():
    req = build_send_request("why is it hot?")

    assert req["jsonrpc"] == "2.0"
    assert req["method"] == "message/send"
    assert extract_text(req["params"]["message"]) == "why is it hot?"


def test_reply_is_returned_as_an_artifact_not_a_status_message():
    """In A2A, artifacts are the durable output of the work; status messages are
    progress commentary. Delegated analysis is output."""
    response = build_task_response("req-1", "Likely bearing wear.")

    assert response["result"]["status"]["state"] == "completed"
    assert response["result"]["artifacts"][0]["parts"][0]["text"] == "Likely bearing wear."
    assert extract_task_text(response) == "Likely bearing wear."


def test_extract_task_text_falls_back_to_a_status_message():
    response = {
        "result": {
            "status": {
                "state": "completed",
                "message": {"parts": [{"kind": "text", "text": "from status"}]},
            }
        }
    }
    assert extract_task_text(response) == "from status"


def test_non_text_parts_are_skipped_not_stringified():
    message = {
        "parts": [
            {"kind": "file", "file": {"uri": "x"}},
            {"kind": "text", "text": "real content"},
        ]
    }
    assert extract_text(message) == "real content"


# --- A2A over real HTTP, against the running app --------------------------


@pytest.fixture
def a2a_client(monkeypatch):
    """A TestClient whose specialist endpoint is backed by a fake model."""

    def _build(provider: FakeProvider):
        monkeypatch.setattr("app.main.build_providers", lambda settings: [provider])
        from app.main import app

        return TestClient(app)

    return _build


def test_agent_card_is_served_at_the_well_known_path(a2a_client):
    provider = FakeProvider("fake", ("claude-haiku-4-5",))
    with a2a_client(provider) as client:
        response = client.get("/a2a/specialist/.well-known/agent-card.json")

    assert response.status_code == 200
    assert response.json()["name"] == "Reliability Specialist"


def test_specialist_answers_a_real_json_rpc_message_send(a2a_client):
    provider = FakeProvider(
        "fake",
        ("claude-haiku-4-5",),
        result=_answer("Likely bearing lubrication failure."),
    )
    with a2a_client(provider) as client:
        response = client.post(
            "/a2a/specialist", json=build_send_request("Pump 3 is at 87.4 degC. Why?")
        )

    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert extract_task_text(body) == "Likely bearing lubrication failure."


def test_specialist_uses_a_cheaper_model_tier_than_the_orchestrator(a2a_client):
    """Delegated sub-analysis should not cost more than doing the work inline."""
    provider = FakeProvider("fake", ("claude-haiku-4-5",), result=_answer("x"))
    with a2a_client(provider) as client:
        client.post("/a2a/specialist", json=build_send_request("why?"))

    assert provider.calls[0].model == "claude-haiku-4-5"


def test_unknown_jsonrpc_method_returns_an_error_object_not_an_http_error(a2a_client):
    """JSON-RPC errors ride in the body; an HTTP error would mean the transport
    failed, which is a different thing."""
    provider = FakeProvider("fake", ("claude-haiku-4-5",))
    with a2a_client(provider) as client:
        response = client.post(
            "/a2a/specialist",
            json={"jsonrpc": "2.0", "id": "1", "method": "message/stream"},
        )

    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32601


def test_message_with_no_text_part_is_an_invalid_params_error(a2a_client):
    provider = FakeProvider("fake", ("claude-haiku-4-5",))
    with a2a_client(provider) as client:
        response = client.post(
            "/a2a/specialist",
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "method": "message/send",
                "params": {"message": {"parts": []}},
            },
        )

    assert response.json()["error"]["code"] == -32602


def test_orchestrator_delegates_over_real_http_to_the_specialist(a2a_client):
    """End to end: the model asks to delegate, the orchestrator makes a genuine
    A2A call into the app, and the specialist's answer comes back as a tool
    result the model can use."""
    provider = FakeProvider(
        "fake",
        ("test-model", "claude-haiku-4-5"),
        script=[
            _plan_result(GOOD_PLAN),
            _tool_result(DELEGATE_TOOL.name, {"question": "Pump 3 at 87.4 degC. Why?"}),
            _answer("The specialist says bearing lubrication failure."),
            # The specialist's own model call:
            _answer("Bearing lubrication failure."),
        ],
    )
    with a2a_client(provider) as client:
        response = client.post(
            "/v1/agent",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Why is pump 3 hot?"}],
            },
        )

    assert response.status_code == 200
    gateway = response.json()["gateway"]
    assert gateway["delegated_to_agent"] is True
    assert gateway["plan_goal"] == "Diagnose pump 3"
    assert gateway["plan_steps"] == ["Check telemetry", "Look up limit"]
    delegate_step = next(
        s for s in gateway["tool_steps"] if s["tool"] == DELEGATE_TOOL.name
    )
    assert delegate_step["is_error"] is False
    assert "lubrication" in delegate_step["result"]
