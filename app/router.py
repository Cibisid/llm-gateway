"""The router — decides which provider serves a request.

READ THIS FILE. With app/providers/base.py it is half of the gateway's core
idea, and it is the file an interviewer is most likely to ask you to explain.

PHASE 1 SCOPE (what this file does today):
    Resolve a requested model id to the one registered provider that declares
    support for it, and execute the call.

The routing rule is deliberately the simplest one that works, but the SHAPE is
the shape Phase 2 needs. `select_candidates()` returns an ordered LIST rather
than a single provider, even though today that list never has more than one
entry. That is not over-engineering — it is the difference between Phase 2
being a change of policy inside one function and Phase 2 being a rewrite of
every caller. Fallback, cost-based preference, and latency-based preference all
become "sort this list differently".

WHAT THIS FILE MUST NEVER DO:
    Import a vendor SDK, or branch on `provider.name`. The moment the router
    knows that Anthropic is different from OpenAI, the abstraction in base.py
    has failed and adding a fourth provider means editing this file.
"""

from __future__ import annotations

import logging
import time

from app.providers.base import Provider, ProviderError, ProviderResult
from app.schemas import ChatCompletionRequest

logger = logging.getLogger(__name__)


class NoProviderAvailable(Exception):
    """No registered provider can serve the requested model.

    Distinct from ProviderError: this is a routing failure that happens before
    any network call, so it maps to a 404 rather than a 502.
    """


class RoutingDecision:
    """What the router chose, and what it cost in wall-clock time.

    Returned alongside the result so app/main.py can report the decision back
    to the caller. Routing that cannot be observed cannot be debugged, and a
    reviewer asking "which provider served this?" should not have to read logs.
    """

    def __init__(self, provider: str, latency_ms: int) -> None:
        self.provider = provider
        self.latency_ms = latency_ms


class Router:
    def __init__(self, providers: list[Provider]) -> None:
        self._providers = providers

    def available_models(self) -> list[str]:
        """Every model any registered provider can serve, deduplicated."""
        return sorted({m for p in self._providers for m in p.supported_models})

    def select_candidates(self, model: str) -> list[Provider]:
        """Providers that can serve `model`, best first.

        Phase 1: registration order, filtered by capability.
        Phase 2: this is where cost/latency/capability preference goes.
        """
        return [p for p in self._providers if p.supports(model)]

    async def route(
        self, request: ChatCompletionRequest
    ) -> tuple[ProviderResult, RoutingDecision]:
        candidates = self.select_candidates(request.model)
        if not candidates:
            known = self.available_models()
            raise NoProviderAvailable(
                f"No configured provider serves model {request.model!r}. "
                f"Available models: {', '.join(known) or 'none — no API keys configured'}"
            )

        provider = candidates[0]

        # Measured around the provider call only, so the number reported to the
        # caller is upstream latency and not our own serialisation overhead.
        started = time.perf_counter()
        try:
            result = await provider.chat(request)
        except ProviderError:
            # Phase 1 has no fallback: one candidate, so a failure is final.
            # Phase 2 walks to candidates[1] when the error is retryable.
            logger.exception("provider %s failed for model %s", provider.name, request.model)
            raise

        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "routed model=%s provider=%s latency_ms=%d tokens_in=%d tokens_out=%d",
            request.model,
            provider.name,
            latency_ms,
            result.usage.input_tokens,
            result.usage.output_tokens,
        )
        return result, RoutingDecision(provider.name, latency_ms)
