"""OpenAI-compatible request/response schemas.

The gateway's public API deliberately mirrors OpenAI's `/v1/chat/completions`
so that any existing OpenAI client library can point at this service by
changing only its base URL. That compatibility is the whole reason the shape
below is not "designed" — it is copied.

Provider-specific quirks are absorbed by the adapters in app/providers/, never
by this schema.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    """A single message in the conversation, OpenAI-shaped.

    Note `system` is a role here. Anthropic instead takes the system prompt as
    a separate top-level argument, so the Anthropic adapter hoists these out.
    """

    role: Literal["system", "user", "assistant"]
    content: str


class ChatCompletionRequest(BaseModel):
    # Reject unknown fields rather than silently ignoring them: a client that
    # sends an option we don't honour should find out immediately, not discover
    # later that the gateway quietly dropped it.
    model_config = ConfigDict(extra="forbid")

    model: str
    messages: list[ChatMessage] = Field(min_length=1)

    max_tokens: int | None = Field(default=None, gt=0, le=128_000)

    # Accepted by the schema (OpenAI clients send it) but NOT forwarded to
    # current Claude models, which reject sampling parameters with a 400.
    # See app/providers/anthropic_provider.py for how this is handled.
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)

    # Declared so we can reject them explicitly in app/main.py rather than
    # accept-and-ignore. A silently ignored `stream: true` is a lie.
    stream: bool = False
    tools: list[dict] | None = None

    # --- gateway extensions (not part of OpenAI's API) ---
    # Opt-in rather than automatic: tool calling costs extra model round trips,
    # so a caller who just wants a completion should not silently pay for them.
    use_mcp_tools: bool = False


class ResponseMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str


class Choice(BaseModel):
    index: int = 0
    message: ResponseMessage
    finish_reason: str


class CompletionUsage(BaseModel):
    """OpenAI's usage field names.

    Adapters report usage in the provider-neutral `Usage` dataclass
    (app/providers/base.py); this is the wire representation of it.
    """

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class RoutingAttempt(BaseModel):
    """One candidate the router tried, and whether it worked."""

    candidate: str
    ok: bool
    error: str | None = None


class ToolStepReport(BaseModel):
    """One tool the gateway ran on the model's behalf."""

    iteration: int
    tool: str
    arguments: dict
    result: str
    is_error: bool


class GatewayMetadata(BaseModel):
    """Non-standard block describing what the gateway itself did.

    Namespaced under one key so it cannot collide with a future OpenAI field.
    This is what makes routing observable: which provider served the request,
    what it cost, whether a fallback was needed, and what failed on the way.
    """

    provider: str
    upstream_model: str
    latency_ms: int

    #: Which ranking rule was in force ("order" | "cost" | "latency").
    routing_strategy: str

    #: Null when the served model has no verified price. See app/pricing.py —
    #: an unknown cost is reported as unknown rather than guessed at.
    cost_usd: float | None = None

    fallback_occurred: bool = False
    attempts: list[RoutingAttempt] = Field(default_factory=list)

    #: Every tool the gateway ran on the model's behalf, in order. Makes an
    #: agentic answer auditable without server logs, and is what the Phase 5
    #: eval grades tool-call correctness against.
    tool_steps: list[ToolStepReport] = Field(default_factory=list)

    #: True when the tool loop was stopped by its iteration cap. The answer is
    #: then potentially incomplete, and says so.
    hit_iteration_cap: bool = False

    # --- orchestrator fields (populated by /v1/agent only) ---
    #: The plan the agent wrote before acting. Surfaced so the intended course
    #: of action is inspectable, and so a human approval gate has something to
    #: gate on.
    plan_goal: str | None = None
    plan_steps: list[str] = Field(default_factory=list)
    #: True when the planner's output could not be parsed. The run still
    #: proceeded, but the plan shown did not guide it — say so rather than
    #: display a plan that was not actually followed.
    plan_malformed: bool = False
    #: True when work was delegated to the specialist agent over A2A.
    delegated_to_agent: bool = False


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: CompletionUsage
    gateway: GatewayMetadata
