"""The router — decides which provider and model serve a request, and what it cost.

READ THIS FILE. With app/providers/base.py it is the core of the gateway, and
it is what an interviewer will ask you to walk through.

THE PROBLEM IT SOLVES
    A client asks for a model. Sometimes that is a specific model
    ("claude-opus-5"), and sometimes it is an intent ("auto" — give me
    something that can answer this, cheaply). The router turns either into an
    ordered list of concrete (provider, model) candidates, tries them in order,
    falls back when one fails in a way worth retrying, and reports what it did.

THE THREE MOVING PARTS

1. CANDIDATE EXPANSION (`select_candidates`)
   A concrete model resolves to every provider that can serve it — which can be
   more than one, e.g. gpt-4o via both OpenAI direct and Azure OpenAI. An alias
   expands to the list of concrete models configured behind it. Either way the
   output is a list of `Candidate`, so the rest of the router does not care
   which kind of request it was.

2. RANKING (`_rank`)
   The strategy decides the order:
     - "cost"    cheapest input price first; unpriced models last, because we
                 refuse to guess at a price (see app/pricing.py)
     - "latency" fastest observed provider first, using an EWMA of real calls
     - "order"   configuration order — predictable, the default
   Ranking is the ONLY thing that changes between strategies. Everything
   downstream is identical, which is why adding a strategy is a small change.

3. EXECUTION WITH FALLBACK (`route`)
   Walk the ranked candidates. On a RETRYABLE failure (429, 5xx, connection
   error) move to the next candidate. On a NON-RETRYABLE failure (400, 401)
   stop immediately — a malformed request will fail identically everywhere, and
   retrying it just burns money and latency on a second guaranteed failure.
   That distinction is made by the adapter, not here; see
   `ProviderError.retryable` in app/providers/base.py.

WHAT THIS FILE MUST NEVER DO
    Import a vendor SDK, or branch on `provider.name`. The moment it does, the
    adapter contract has failed and adding a provider means editing the router.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.pricing import compute_cost_usd, estimated_input_cost_usd
from app.providers.base import (
    Provider,
    ProviderError,
    ProviderResult,
    ToolSpec,
    Turn,
)
from app.schemas import ChatCompletionRequest

logger = logging.getLogger(__name__)

# Weight given to the newest observation in the latency EWMA. 0.3 keeps the
# estimate responsive to a provider degrading without letting one slow request
# dominate the ranking.
_LATENCY_SMOOTHING = 0.3


class NoProviderAvailable(Exception):
    """No registered provider can serve the requested model.

    Distinct from ProviderError: this is a routing failure that happens before
    any network call, so it maps to a 404 rather than a 502.
    """


@dataclass(frozen=True)
class Candidate:
    """One concrete way to serve a request: a provider plus the model it runs."""

    provider: Provider
    model: str

    def __str__(self) -> str:
        return f"{self.provider.name}/{self.model}"


@dataclass
class Attempt:
    """A single try against one candidate — succeeded or not.

    Recorded so that a fallback is visible in the response rather than being an
    invisible act of mercy. Debugging "why was this slow?" needs the failures,
    not just the eventual success.
    """

    candidate: str
    ok: bool
    error: str | None = None


@dataclass
class RoutingDecision:
    """Everything the router did, reported back to the caller.

    Routing that cannot be observed cannot be debugged, and a reviewer asking
    "which provider served this, and what did it cost?" should not have to read
    a log file to find out.
    """

    provider: str
    model: str
    strategy: str
    latency_ms: int
    cost_usd: float | None
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def fallback_occurred(self) -> bool:
        return len(self.attempts) > 1


class LatencyTracker:
    """Exponentially weighted moving average of per-provider latency.

    In-memory and per-process: good enough to steer routing within one
    instance, and deliberately not a distributed store. Sharing this across
    replicas would be a real design decision (Redis, or accepting per-instance
    estimates); it is out of scope here and noted rather than pretended away.
    """

    def __init__(self) -> None:
        self._ewma: dict[str, float] = {}

    def record(self, provider: str, latency_ms: float) -> None:
        previous = self._ewma.get(provider)
        if previous is None:
            self._ewma[provider] = latency_ms
        else:
            self._ewma[provider] = (
                _LATENCY_SMOOTHING * latency_ms + (1 - _LATENCY_SMOOTHING) * previous
            )

    def get(self, provider: str) -> float | None:
        return self._ewma.get(provider)

    def snapshot(self) -> dict[str, float]:
        return dict(self._ewma)


class Router:
    def __init__(
        self,
        providers: list[Provider],
        *,
        strategy: str = "order",
        aliases: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._providers = providers
        self._strategy = strategy
        self._aliases = aliases or {}
        self.latency = LatencyTracker()

    # --- introspection -----------------------------------------------------

    @property
    def strategy(self) -> str:
        return self._strategy

    def available_models(self) -> list[str]:
        """Concrete models any registered provider can serve, deduplicated."""
        return sorted({m for p in self._providers for m in p.supported_models})

    def available_aliases(self) -> list[str]:
        """Aliases with at least one reachable member.

        An alias whose every member needs a key we don't have is not offered,
        so `/healthz` never advertises something that would 404.
        """
        servable = set(self.available_models())
        return sorted(a for a, members in self._aliases.items() if servable & set(members))

    # --- 1. candidate expansion -------------------------------------------

    def select_candidates(self, model: str) -> list[Candidate]:
        """Concrete (provider, model) pairs that can serve `model`, best first."""
        if model in self._aliases:
            candidates = [
                Candidate(provider, member)
                for member in self._aliases[model]
                for provider in self._providers
                if provider.supports(member)
            ]
        else:
            candidates = [
                Candidate(provider, model)
                for provider in self._providers
                if provider.supports(model)
            ]
        return self._rank(candidates)

    # --- 2. ranking --------------------------------------------------------

    def _rank(self, candidates: list[Candidate]) -> list[Candidate]:
        if self._strategy == "cost":
            # Unpriced models sort last rather than being treated as free — the
            # cheapest-looking option must never be one we simply can't price.
            def cost_key(c: Candidate) -> tuple[int, float]:
                price = estimated_input_cost_usd(c.model)
                return (1, 0.0) if price is None else (0, price)

            return sorted(candidates, key=cost_key)

        if self._strategy == "latency":
            # Never-used providers sort FIRST, on purpose: with no observation
            # they would otherwise never be tried, and the ranking would freeze
            # on whichever provider happened to be measured first.
            def latency_key(c: Candidate) -> tuple[int, float]:
                observed = self.latency.get(c.provider.name)
                return (0, 0.0) if observed is None else (1, observed)

            return sorted(candidates, key=latency_key)

        # "order": configuration order. Predictable, and the right default when
        # you have not yet measured anything.
        return candidates

    # --- 3. execution with fallback ---------------------------------------

    async def route(
        self,
        request: ChatCompletionRequest,
        *,
        tools: Sequence[ToolSpec] = (),
        extra_turns: Sequence[Turn] = (),
    ) -> tuple[ProviderResult, RoutingDecision]:
        candidates = self.select_candidates(request.model)

        if tools:
            # A provider that cannot run tools must not be a fallback target
            # here. Falling back to one would silently drop the tools and
            # return a confident, ungrounded answer — the worst failure mode
            # available, because it looks like success.
            tool_capable = [c for c in candidates if c.provider.supports_tools]
            if candidates and not tool_capable:
                raise NoProviderAvailable(
                    f"Model {request.model!r} is available, but no provider "
                    "serving it supports tool calling."
                )
            candidates = tool_capable

        if not candidates:
            raise NoProviderAvailable(self._unavailable_message(request.model))

        attempts: list[Attempt] = []
        last_error: ProviderError | None = None

        for candidate in candidates:
            # The upstream call uses the CONCRETE model, which differs from what
            # the client asked for whenever an alias was used.
            upstream_request = request.model_copy(update={"model": candidate.model})

            # Timed around the provider call only, so the reported number is
            # upstream latency and not our own serialisation overhead.
            started = time.perf_counter()
            try:
                result = await candidate.provider.chat(
                    upstream_request, tools=tools, extra_turns=extra_turns
                )
            except ProviderError as exc:
                elapsed_ms = (time.perf_counter() - started) * 1000
                attempts.append(Attempt(str(candidate), ok=False, error=str(exc)))
                last_error = exc

                if not exc.retryable:
                    # A malformed request fails identically everywhere. Trying
                    # the next provider would waste a call to reach the same 400.
                    logger.warning(
                        "non-retryable failure on %s; not falling back", candidate
                    )
                    raise

                # Record the failure's latency too — a provider that is timing
                # out is slow, and the latency strategy should learn that.
                self.latency.record(candidate.provider.name, elapsed_ms)
                logger.warning("retryable failure on %s; trying next candidate", candidate)
                continue

            latency_ms = int((time.perf_counter() - started) * 1000)
            self.latency.record(candidate.provider.name, latency_ms)
            attempts.append(Attempt(str(candidate), ok=True))

            # Cost is computed from the CONCRETE model that actually ran, not
            # the alias the client asked for — an alias has no price.
            cost_usd = compute_cost_usd(candidate.model, result.usage)

            decision = RoutingDecision(
                provider=candidate.provider.name,
                model=candidate.model,
                strategy=self._strategy,
                latency_ms=latency_ms,
                cost_usd=cost_usd,
                attempts=attempts,
            )
            logger.info(
                "routed requested=%s served=%s strategy=%s latency_ms=%d "
                "tokens_in=%d tokens_out=%d cost_usd=%s fallback=%s",
                request.model,
                candidate,
                self._strategy,
                latency_ms,
                result.usage.input_tokens,
                result.usage.output_tokens,
                cost_usd if cost_usd is not None else "unpriced",
                decision.fallback_occurred,
            )
            return result, decision

        # Every candidate failed retryably.
        logger.error(
            "all %d candidates failed for model %s", len(attempts), request.model
        )
        assert last_error is not None
        raise last_error

    def _unavailable_message(self, model: str) -> str:
        models = self.available_models()
        if not models:
            return (
                f"No configured provider serves model {model!r}. "
                "No API keys are configured, so no models are available."
            )
        aliases = self.available_aliases()
        detail = f"Available models: {', '.join(models)}"
        if aliases:
            detail += f". Available aliases: {', '.join(aliases)}"
        return f"No configured provider serves model {model!r}. {detail}"
