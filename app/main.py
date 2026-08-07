"""FastAPI application — the gateway's public surface.

SECURITY POSTURE (Phase 6)
    Every model-invoking endpoint requires `Authorization: Bearer <key>` and is
    rate limited per key. The service fails CLOSED: with no keys configured it
    refuses everything with 503 rather than running open.

    `/healthz` is deliberately unauthenticated — a liveness probe that needs a
    credential is a liveness probe that fails during a credential outage. It
    exposes capability, never configuration or secrets.

    Every request writes one structured audit record. Prompt and response
    content is NOT logged unless AUDIT_LOG_CONTENT is explicitly enabled.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request

from agents.specialist_agent import agent_card, handle_task
from app.a2a import build_error_response, build_task_response, extract_text
from app.config import MODEL_ALIASES, get_settings
from app.logging_setup import (
    attach_content,
    configure_logging,
    new_record,
    write_audit,
)
from app.orchestrator import Orchestrator
from app.mcp_client import MCPToolbox
from app.security import (
    AuthContext,
    RateLimiter,
    parse_api_keys,
    require_api_key,
)
from app.providers.base import ProviderError, ProviderResult
from app.providers.registry import build_providers
from app.router import NoProviderAvailable, Router
from app.tool_loop import run_tool_loop
from mcp_server.server import server as mcp_app
from app.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    CompletionUsage,
    GatewayMetadata,
    ResponseMessage,
    RoutingAttempt,
    ToolStepReport,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Providers are built once at startup, not per request: each adapter owns a
    # pooled HTTP client, and rebuilding them per request would leak sockets.
    settings = get_settings()
    configure_logging(settings.log_level, settings.audit_log_path or None)

    app.state.settings = settings
    app.state.api_keys = parse_api_keys(settings.gateway_api_keys)
    app.state.rate_limiter = RateLimiter(limit=settings.rate_limit_per_minute)
    if not app.state.api_keys:
        logger.warning(
            "GATEWAY_API_KEYS is empty — every request will be refused with 503"
        )

    app.state.router = Router(
        build_providers(settings),
        strategy=settings.routing_strategy,
        aliases=MODEL_ALIASES,
    )

    # One MCP connection for the process lifetime. Reconnecting per request
    # would pay the initialize + tool-discovery round trip on every message.
    async with MCPToolbox(mcp_app) as toolbox:
        app.state.toolbox = toolbox

        # The orchestrator reaches the specialist over HTTP even though both
        # are served by this process. That is deliberate: the specialist is a
        # separate agent that happens to be co-located, and moving it to
        # another host must be a URL change and nothing more.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=settings.self_base_url,
            timeout=60.0,
        ) as http_client:
            app.state.http_client = http_client
            yield


app = FastAPI(
    title="AI Gateway",
    description="OpenAI-compatible gateway routing across multiple LLM providers.",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/healthz")
async def healthz(request: Request) -> dict:
    """Liveness probe. Reports which models are actually reachable.

    Lists real capability rather than a bare {"status": "ok"} so a misconfigured
    deployment (no keys mounted) is visible from the probe instead of only
    surfacing on the first real request.
    """
    router: Router = request.app.state.router
    return {
        "status": "ok",
        "available_models": router.available_models(),
        "available_aliases": router.available_aliases(),
        "routing_strategy": router.strategy,
        # Observed per-provider latency the router is currently steering on.
        # Empty until real traffic has been served.
        "observed_latency_ms": router.latency.snapshot(),
        "mcp_tools": [s.name for s in request.app.state.toolbox.specs],
    }


# --- A2A: the specialist agent's public surface --------------------------


@app.get("/a2a/specialist/.well-known/agent-card.json")
async def specialist_agent_card(request: Request) -> dict:
    """A2A discovery. Another agent reads this to decide whether to delegate."""
    settings = get_settings()
    return agent_card(settings.self_base_url).to_dict()


@app.post("/a2a/specialist")
async def specialist_endpoint(
    request: Request, auth: AuthContext = Depends(require_api_key)
) -> dict:
    """JSON-RPC 2.0 `message/send`.

    Errors are returned as JSON-RPC error objects with HTTP 200, per the
    JSON-RPC convention — an HTTP error code would mean the *transport* failed,
    which is a different thing from the method failing.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return build_error_response(None, -32700, "Parse error")

    request_id = body.get("id")
    method = body.get("method")
    if method != "message/send":
        return build_error_response(
            request_id, -32601, f"Method not found: {method!r}"
        )

    message = (body.get("params") or {}).get("message") or {}
    question = extract_text(message)
    if not question:
        return build_error_response(
            request_id, -32602, "Invalid params: message has no text part"
        )

    audit = new_record(
        request_id=str(request_id),
        key_id=auth.key_id,
        endpoint="/a2a/specialist",
        requested_model="(specialist)",
    )
    started = time.perf_counter()

    try:
        answer = await handle_task(question, request.app.state.router)
    except (NoProviderAvailable, ProviderError) as exc:
        # -32000 is the JSON-RPC reserved range for implementation-defined
        # server errors, which an upstream model failure is.
        _audit_failure(audit, 200, str(exc), started)
        return build_error_response(request_id, -32000, str(exc))

    audit.latency_ms = int((time.perf_counter() - started) * 1000)
    write_audit(audit)
    return build_task_response(request_id, answer)


# --- the orchestrated agent endpoint -------------------------------------


@app.post("/v1/agent", response_model=ChatCompletionResponse)
async def agent(
    body: ChatCompletionRequest,
    request: Request,
    auth: AuthContext = Depends(require_api_key),
) -> ChatCompletionResponse:
    """Plan -> act -> observe, with tools and A2A delegation.

    Deliberately a separate route from /v1/chat/completions rather than another
    flag on it: an agent run has different cost, latency, and failure
    characteristics from a completion, and a caller should have to opt into
    that explicitly by choosing a different endpoint.
    """
    if body.stream:
        raise HTTPException(status_code=400, detail="Streaming is not implemented.")

    settings = request.app.state.settings
    request_id = f"chatcmpl-{uuid.uuid4().hex}"
    audit = new_record(
        request_id=request_id,
        key_id=auth.key_id,
        endpoint="/v1/agent",
        requested_model=body.model,
    )
    started = time.perf_counter()

    orchestrator = Orchestrator(
        request.app.state.router,
        request.app.state.toolbox,
        specialist_url="/a2a/specialist",
        http_client=request.app.state.http_client,
        auth_header=request.headers.get("authorization"),
    )

    try:
        outcome = await orchestrator.run(body)
    except NoProviderAvailable as exc:
        _audit_failure(audit, 404, str(exc), started)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProviderError as exc:
        _audit_failure(audit, 502, str(exc), started)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    audit.served_model = outcome.model
    audit.provider = outcome.provider
    audit.input_tokens = outcome.usage.input_tokens
    audit.output_tokens = outcome.usage.output_tokens
    audit.cost_usd = outcome.total_cost_usd
    audit.latency_ms = int((time.perf_counter() - started) * 1000)
    audit.tools_called = [s.tool for s in outcome.steps]
    audit.delegated = outcome.delegated
    audit.hit_iteration_cap = outcome.hit_step_cap
    attach_content(
        audit,
        prompt=_last_user_text(body),
        response=outcome.text,
        enabled=settings.audit_log_content,
    )
    write_audit(audit)

    return ChatCompletionResponse(
        id=request_id,
        created=int(time.time()),
        model=body.model,
        choices=[
            Choice(
                message=ResponseMessage(content=outcome.text), finish_reason="stop"
            )
        ],
        usage=CompletionUsage(
            prompt_tokens=outcome.usage.input_tokens,
            completion_tokens=outcome.usage.output_tokens,
            total_tokens=outcome.usage.total_tokens,
        ),
        gateway=GatewayMetadata(
            provider=outcome.provider or "unknown",
            upstream_model=outcome.model or body.model,
            latency_ms=0,
            routing_strategy=request.app.state.router.strategy,
            cost_usd=outcome.total_cost_usd,
            tool_steps=[
                ToolStepReport(
                    iteration=s.iteration,
                    tool=s.tool,
                    arguments=s.arguments,
                    result=s.result,
                    is_error=s.is_error,
                )
                for s in outcome.steps
            ],
            hit_iteration_cap=outcome.hit_step_cap,
            # The plan is surfaced so "what was this agent about to do?" is
            # answerable without server logs — and is where a human approval
            # gate would sit in a real deployment.
            plan_goal=outcome.plan.goal,
            plan_steps=outcome.plan.steps,
            plan_malformed=outcome.plan.malformed,
            delegated_to_agent=outcome.delegated,
        ),
    )


def _last_user_text(body: ChatCompletionRequest) -> str:
    for message in reversed(body.messages):
        if message.role == "user":
            return message.content
    return ""


def _audit_failure(audit, status_code: int, error: str, started: float) -> None:
    """Failures are audited too.

    An audit trail that only records successes cannot answer "was this ever
    refused?" — which is exactly the question a compliance review asks.
    """
    audit.status_code = status_code
    audit.error = error
    audit.latency_ms = int((time.perf_counter() - started) * 1000)
    write_audit(audit)


@app.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    auth: AuthContext = Depends(require_api_key),
) -> ChatCompletionResponse:
    # Rejected explicitly rather than ignored. Accepting `stream: true` and
    # returning a non-streamed body would be a lie in the shape of a feature.
    if body.stream:
        raise HTTPException(
            status_code=400,
            detail="Streaming is not implemented in this phase. Omit `stream` or set it to false.",
        )
    if body.tools:
        raise HTTPException(
            status_code=400,
            detail=(
                "Client-supplied tool definitions are not supported. Set "
                "`use_mcp_tools: true` to use the gateway's MCP tools instead."
            ),
        )

    router: Router = request.app.state.router
    toolbox: MCPToolbox = request.app.state.toolbox
    settings = request.app.state.settings

    request_id = f"chatcmpl-{uuid.uuid4().hex}"
    audit = new_record(
        request_id=request_id,
        key_id=auth.key_id,
        endpoint="/v1/chat/completions",
        requested_model=body.model,
    )
    started = time.perf_counter()

    tool_steps: list[ToolStepReport] = []
    hit_cap = False

    try:
        if body.use_mcp_tools:
            loop_result = await run_tool_loop(body, router, toolbox)
            # The loop makes several model calls; report the summed usage and
            # cost, not just those of the final one.
            result = ProviderResult(
                text=loop_result.text,
                upstream_model=loop_result.decision.model
                if loop_result.decision
                else body.model,
                usage=loop_result.usage,
                finish_reason="stop",
            )
            decision = loop_result.decision
            assert decision is not None
            decision.cost_usd = loop_result.total_cost_usd
            hit_cap = loop_result.hit_iteration_cap
            tool_steps = [
                ToolStepReport(
                    iteration=s.iteration,
                    tool=s.tool,
                    arguments=s.arguments,
                    result=s.result,
                    is_error=s.is_error,
                )
                for s in loop_result.steps
            ]
        else:
            result, decision = await router.route(body)
    except NoProviderAvailable as exc:
        _audit_failure(audit, 404, str(exc), started)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProviderError as exc:
        # 502: the gateway is fine, the upstream it depends on is not.
        _audit_failure(audit, 502, str(exc), started)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    audit.served_model = result.upstream_model
    audit.provider = decision.provider
    audit.input_tokens = result.usage.input_tokens
    audit.output_tokens = result.usage.output_tokens
    audit.cost_usd = decision.cost_usd
    audit.latency_ms = int((time.perf_counter() - started) * 1000)
    audit.tools_called = [s.tool for s in tool_steps]
    audit.fallback_occurred = decision.fallback_occurred
    audit.hit_iteration_cap = hit_cap
    attach_content(
        audit,
        prompt=_last_user_text(body),
        response=result.text,
        enabled=settings.audit_log_content,
    )
    write_audit(audit)

    # Envelope assembly lives here, once, rather than in each adapter — see the
    # rationale in app/providers/base.py.
    return ChatCompletionResponse(
        id=request_id,
        created=int(time.time()),
        model=body.model,
        choices=[
            Choice(
                message=ResponseMessage(content=result.text),
                finish_reason=result.finish_reason,
            )
        ],
        usage=CompletionUsage(
            prompt_tokens=result.usage.input_tokens,
            completion_tokens=result.usage.output_tokens,
            total_tokens=result.usage.total_tokens,
        ),
        gateway=GatewayMetadata(
            provider=decision.provider,
            upstream_model=result.upstream_model,
            latency_ms=decision.latency_ms,
            routing_strategy=decision.strategy,
            cost_usd=decision.cost_usd,
            fallback_occurred=decision.fallback_occurred,
            attempts=[
                RoutingAttempt(candidate=a.candidate, ok=a.ok, error=a.error)
                for a in decision.attempts
            ],
            tool_steps=tool_steps,
            hit_iteration_cap=hit_cap,
        ),
    )
