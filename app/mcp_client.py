"""The gateway as an MCP host/client.

Discovers tools from an MCP server, converts them into the provider-neutral
`ToolSpec`, and executes calls on the model's behalf.

WHY IN-PROCESS RATHER THAN OVER STDIO
    `Client(server)` connects to an `MCPServer` object directly, with no
    subprocess and no socket. The same code with a URL instead
    (`Client("https://.../mcp")`) talks to a remote server — the SDK makes the
    transport a parameter, not a rewrite.

    In-process is the right default here: the tools ship with the gateway, so
    spawning a subprocess to reach them would add failure modes (process
    lifetime, zombie cleanup, stream framing) that buy nothing. It also lets
    the Phase 5 eval harness run real tool calls in CI with no orchestration.
    `mcp_server/server.py` still exposes `server.run()` for stdio, so the same
    server can be attached to Claude Desktop or an IDE unchanged.

SECURITY POSTURE
    Tool RESULTS ARE UNTRUSTED INPUT. A manual section or a telemetry payload
    could contain text engineered to look like an instruction ("ignore previous
    instructions and ..."). The gateway never interprets tool output as
    instructions — it goes back to the model as data, inside a tool_result
    block, and the system prompt tells the model to treat it as reference
    material. This is a real prompt-injection surface and Phase 6 revisits it.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp import Client

from app.providers.base import ToolOutcome, ToolSpec

logger = logging.getLogger(__name__)


class MCPToolbox:
    """Owns the MCP connection and exposes it in gateway terms.

    Held open for the lifetime of the app rather than reconnected per request:
    the MCP handshake (initialize, capability exchange, tool discovery) costs a
    round trip that would otherwise be paid on every user message.
    """

    def __init__(self, server: Any) -> None:
        self._server = server
        self._client: Client | None = None
        self._specs: tuple[ToolSpec, ...] = ()

    async def __aenter__(self) -> MCPToolbox:
        self._client = await Client(self._server).__aenter__()
        await self._discover()
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._client is not None:
            await self._client.__aexit__(*exc_info)
            self._client = None

    async def _discover(self) -> None:
        assert self._client is not None
        listed = await self._client.list_tools()
        # MCP's tool shape and ToolSpec are deliberately near-identical, so this
        # is a rename rather than a translation. Each adapter then reshapes it
        # into its own vendor's envelope.
        self._specs = tuple(
            ToolSpec(
                name=tool.name,
                description=tool.description or "",
                input_schema=tool.input_schema,
            )
            for tool in listed.tools
        )
        logger.info("MCP tools discovered: %s", [s.name for s in self._specs])

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return self._specs

    def has(self, name: str) -> bool:
        return any(spec.name == name for spec in self._specs)

    async def invoke(self, call_id: str, name: str, arguments: dict) -> ToolOutcome:
        """Run one tool and package the result for the model.

        EVERY failure path returns a ToolOutcome with is_error=True rather than
        raising. A model that is told "that tool does not exist, here are the
        ones that do" will correct itself on the next turn; an exception ends
        the conversation and returns a 500 to a user whose question was fine.
        """
        if self._client is None:
            return ToolOutcome(call_id, "Tool system unavailable.", is_error=True)

        if not self.has(name):
            available = ", ".join(s.name for s in self._specs) or "none"
            return ToolOutcome(
                call_id,
                f"No such tool {name!r}. Available tools: {available}.",
                is_error=True,
            )

        try:
            result = await self._client.call_tool(name, arguments)
        except Exception as exc:  # noqa: BLE001 - deliberately broad; see docstring
            logger.exception("MCP tool %s raised", name)
            return ToolOutcome(call_id, f"Tool {name!r} failed: {exc}", is_error=True)

        return ToolOutcome(
            call_id,
            _render_result(result),
            # MCP reports a tool's own failure via is_error rather than by
            # raising — e.g. schema validation on the arguments. That must reach
            # the model, or it will treat an error message as a real answer.
            is_error=bool(result.is_error),
        )


def _render_result(result: Any) -> str:
    """Flatten an MCP CallToolResult into text for the model.

    Prefers `structured_content` when the tool declared an output schema, and
    falls back to the text blocks otherwise. Both shapes occur in practice: a
    scalar return produces structured content, while an un-annotated dict
    return arrives only as JSON text.
    """
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return json.dumps(structured, indent=2, sort_keys=True, default=str)

    parts = [
        block.text
        for block in (result.content or [])
        if getattr(block, "type", None) == "text"
    ]
    return "\n".join(parts) if parts else "(tool returned no content)"
