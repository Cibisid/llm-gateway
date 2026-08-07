# Phase 3 — Tool calling over MCP

## What it does

Stands up an MCP server exposing three domain tools, makes the gateway an MCP
host/client, and runs the tool-calling loop on the model's behalf. A request
with `"use_mcp_tools": true` gets a grounded answer plus a full trace of which
tools ran.

```
POST /v1/chat/completions  {"use_mcp_tools": true}
   │
   └─ tool_loop ─┬─► router ─► provider ─► model
                 │      ▲                    │
                 │      └── tool results ────┤ "call get_telemetry(pump-3)"
                 │                           ▼
                 └─► MCPToolbox ─► MCP client ─► MCP server ─► telemetry / manuals
```

## The version decision the plan flagged

The build plan assumed `mcp` 1.x and suggested pinning `mcp>=1.28,<2`. That
turned out to be stale: **2.0.0 is now the stable line**, it implements the
2026-07-28 MCP specification — the exact spec the plan names as current — and
1.x has moved to critical-fixes-and-security-patches only.

Pinned `mcp==2.0.0`. Building new work on a maintenance branch would be the
wrong call for a project meant to demonstrate current practice. The v2 API was
confirmed by introspection and a live round trip before any code was written
against it, not from memory — v2 also renamed `inputSchema` to `input_schema`,
which a from-memory implementation would have got wrong.

## The one interesting decision

**Tool results are untrusted input, and the contract says so.**

A retrieved manual section or a telemetry payload is *data the model reasons
about*, never *instructions the model obeys*. If an attacker could get text into
the manuals corpus, `"ignore previous instructions and ..."` would otherwise be
a live prompt-injection path straight into an agent that can call tools.

Two things enforce this: results go back inside `tool_result` blocks (never
concatenated into the prompt as if the user said them), and the system prompt
states the rule explicitly. This is a real attack surface that Phase 6 revisits;
it is called out here rather than discovered later.

## Why the Provider contract had to grow

Tool calling is the first thing that genuinely forced `base.py` to change, and
the reason is worth knowing: **the two vendors model the same concept
incompatibly.**

|  | Anthropic | OpenAI / Azure |
|---|---|---|
| Tool definition | flat, `input_schema` | wrapped in `{"type":"function"}`, `parameters` |
| Arguments arrive as | a parsed object | a JSON **string** that must be parsed (and can be malformed) |
| Tool result goes in | `tool_result` blocks on a **user** message | a dedicated `role: "tool"` message, one per result |

Letting either shape leak upward would have put vendor-shaped dicts into the
orchestrator. So the contract gained a neutral `Turn` / `ToolCall` /
`ToolOutcome` vocabulary, and each adapter translates. The router still contains
no vendor-specific code.

The OpenAI and Azure adapters now share `_openai_compatible.py` — they speak an
identical wire format and differ only in client construction and model
addressing. Azure's entire difference is a one-line `_upstream_model()`
override, which is the evidence the factoring is right.

## Three things that make the loop safe to run

1. **A hard iteration cap.** A model can call a tool, dislike the answer, and
   call again indefinitely. Without a ceiling one request spends unbounded
   money. When the cap is hit the answer is **labelled incomplete** — a
   truncated agent run presented as a finished answer is how you ship something
   confidently wrong.
2. **Tool failures go to the model, not the client.** An unknown tool or a bad
   argument returns a `tool_result` carrying the error, and the model gets a
   turn to correct itself. Every error path names the valid alternatives,
   because a model told only "unknown asset" will invent one.
3. **The router refuses to fall back to a tool-incapable provider.** Silently
   dropping the tools would produce a confident, ungrounded answer — the worst
   available failure mode, because it looks like success.

## Retrieval: lexical, deliberately

The manuals tool uses IDF-weighted token overlap, not embeddings. No API key, no
network, fully deterministic — so Phase 5 can assert exact retrieval results in
CI, and a failing eval means the code changed rather than a model drifted. Over
a six-section corpus, recall is not the interesting problem; the grounding
contract is. Every result carries a `section_id` so a claim can be traced back.

A production corpus would use a vector index. The tool interface would not
change — which is the point of putting retrieval behind a tool.

## Honest status

- The MCP server, client, discovery, and tool execution are **real and tested
  against the actual protocol** (in-process transport, genuine round trip).
- The tool *loop* is tested with a scripted fake model. It has not been driven
  by a live Claude yet — that needs the API key.
- `mcp_server/server.py` still exposes `server.run()` for stdio, so the same
  server attaches to Claude Desktop or an IDE unchanged.

## How to run it

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions -H "content-type: application/json" -d "{\"model\":\"claude-sonnet-5\",\"use_mcp_tools\":true,\"messages\":[{\"role\":\"user\",\"content\":\"Is pump 3 running too hot, and what does the manual say to do?\"}]}"
```

The `gateway.tool_steps` array reports every tool that ran, with its arguments
and its result.

## Tests

`python -m pytest -q` — 84 tests. Phase 3 adds the tools' own behaviour,
real-protocol discovery and invocation, error handling, the loop (including the
iteration cap and usage summing), and the router's tool-capability guard.
