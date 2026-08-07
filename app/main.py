"""FastAPI application — the gateway's public surface.

SECURITY NOTE FOR PHASE 1: this endpoint has NO AUTHENTICATION. Anyone who can
reach the port can spend your provider credits. Auth, rate limiting, and audit
logging arrive in Phase 6. Until then, bind to localhost only.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request

from app.config import MODEL_ALIASES, get_settings
from app.providers.base import ProviderError
from app.providers.registry import build_providers
from app.router import NoProviderAvailable, Router
from app.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    CompletionUsage,
    GatewayMetadata,
    ResponseMessage,
    RoutingAttempt,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Providers are built once at startup, not per request: each adapter owns a
    # pooled HTTP client, and rebuilding them per request would leak sockets.
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    app.state.router = Router(
        build_providers(settings),
        strategy=settings.routing_strategy,
        aliases=MODEL_ALIASES,
    )
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
    }


@app.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(
    body: ChatCompletionRequest, request: Request
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
            detail="Tool calling is not implemented in this phase (arrives in Phase 3).",
        )

    router: Router = request.app.state.router

    try:
        result, decision = await router.route(body)
    except NoProviderAvailable as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProviderError as exc:
        # 502: the gateway is fine, the upstream it depends on is not.
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # Envelope assembly lives here, once, rather than in each adapter — see the
    # rationale in app/providers/base.py.
    return ChatCompletionResponse(
        id=f"chatcmpl-{uuid.uuid4().hex}",
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
        ),
    )
