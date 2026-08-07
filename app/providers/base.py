"""The Provider contract — the single most important interface in this project.

READ THIS FILE FIRST. Everything else is downstream of the decisions here.

The gateway must talk to several LLM vendors whose SDKs disagree about almost
everything: how the system prompt is passed, whether `max_tokens` is optional,
what a "stop reason" is called, and what the token-usage fields are named.
This module defines the one shape the rest of the gateway is allowed to see.

Two design decisions carry the most weight:

1. `chat()` returns PRIMITIVES, not a finished OpenAI response envelope.
   Assembling the envelope (id, timestamps, `object: "chat.completion"`) is
   identical for every provider, so it happens once in app/main.py instead of
   being copy-pasted into each adapter. An adapter's only job is translation.

2. Usage is NORMALISED AT THE ADAPTER BOUNDARY into `Usage`.
   Anthropic reports `input_tokens` / `output_tokens`; OpenAI reports
   `prompt_tokens` / `completion_tokens`. If that difference leaked past the
   adapter, every downstream consumer — cost tracking (Phase 2), the audit log
   (Phase 6), the eval harness (Phase 5) — would need its own `if provider ==`
   branch. Normalising here is what keeps cost tracking provider-agnostic.
   This is the seam that makes the whole thing work.

To add a provider: subclass `Provider`, implement `chat()`, register it in
app/providers/registry.py. Do not touch app/router.py.
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from app.schemas import ChatCompletionRequest


# --- Tool calling (added in Phase 3) --------------------------------------
#
# Tool calling forced the only real extension to this contract so far. The
# alternative — letting each adapter expose its vendor's tool format — would
# have put vendor-shaped dicts into the orchestrator, which is exactly the
# coupling base.py exists to prevent.


@dataclass(frozen=True)
class ToolSpec:
    """A tool offered to the model, in JSON Schema terms.

    Deliberately identical in spirit to what MCP's `list_tools` returns, so the
    MCP-to-provider conversion is a rename rather than a translation.
    """

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """A model's request to run a tool.

    `id` matters: a model may request several tools in one turn, and every
    result must be matched back to the call that asked for it. Both vendors
    reject a conversation where a call has no matching result.
    """

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolOutcome:
    """The result of running one tool, on its way back to the model."""

    tool_call_id: str
    content: str
    #: Tool failures are reported to the MODEL, not raised. A model told that a
    #: tool errored will usually correct itself (fix an argument, try another
    #: tool); an exception just ends the conversation.
    is_error: bool = False


@dataclass(frozen=True)
class Turn:
    """One provider-neutral conversation turn beyond the client's messages.

    The tool loop appends these: an assistant turn carrying tool calls, then a
    tool turn carrying their outcomes. Adapters translate them into whatever
    their vendor's wire format demands.
    """

    role: Literal["assistant", "tool"]
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_outcomes: tuple[ToolOutcome, ...] = ()


@dataclass(frozen=True)
class Usage:
    """Provider-neutral token accounting.

    Frozen because usage is a record of what happened; nothing downstream has
    any business mutating it.
    """

    input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class ProviderResult:
    """What every adapter returns, regardless of vendor."""

    text: str
    # The model string the provider actually served. May differ from what the
    # client asked for (aliases, deployment names), so we report both.
    upstream_model: str
    usage: Usage
    # Normalised to OpenAI's vocabulary: "stop" | "length" | "tool_calls" |
    # "content_filter". Each adapter maps its vendor's terms onto this.
    finish_reason: str

    #: Tools the model wants run before it can answer. Empty on a plain reply.
    #: When non-empty, `finish_reason` is "tool_calls".
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)


class ProviderError(Exception):
    """Raised when an upstream call fails.

    `retryable` is the field the Phase 2 router keys its fallback logic on: a
    rate limit or a 503 is worth trying elsewhere, a malformed request is not.
    Classifying the error is the adapter's job, because only the adapter knows
    what its vendor's exception types mean.
    """

    def __init__(self, provider: str, message: str, *, retryable: bool) -> None:
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.retryable = retryable


class Provider(abc.ABC):
    """Abstract base every LLM integration implements."""

    #: Stable identifier used in config, logs, and the `gateway` response block.
    name: str

    #: Model ids this adapter can serve. The router uses this to resolve a
    #: requested model to a provider without knowing anything about vendors.
    supported_models: tuple[str, ...] = ()

    def supports(self, model: str) -> bool:
        return model in self.supported_models

    #: Whether this adapter implements tool calling. The tool loop checks it so
    #: an unsupported provider fails with a clear message rather than silently
    #: ignoring the tools it was handed.
    supports_tools: bool = False

    @abc.abstractmethod
    async def chat(
        self,
        request: ChatCompletionRequest,
        *,
        tools: Sequence[ToolSpec] = (),
        extra_turns: Sequence[Turn] = (),
    ) -> ProviderResult:
        """Translate `request`, call the vendor, translate the reply back.

        `extra_turns` are appended AFTER `request.messages` — they carry the
        tool-calling conversation the gateway built up internally, which the
        client never sees. Keeping them separate means the client's request
        stays the client's request, and a plain call needs neither argument.

        Must raise `ProviderError` (never a vendor-specific exception) so the
        router can reason about failures without importing any vendor SDK.
        """
        raise NotImplementedError
