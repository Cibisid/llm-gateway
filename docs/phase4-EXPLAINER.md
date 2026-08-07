# Phase 4 — Agent orchestration + A2A

## What it does

Adds `POST /v1/agent`: a plan → act → observe loop that uses the MCP tools and
can delegate to a **second, separate agent** over Agent2Agent.

```
POST /v1/agent
   │
   ├─ 1. PLAN     model writes {"goal", "steps"} — no tools offered
   │              (returned to the caller before anything runs)
   │
   ├─ 2. ACT      loop: model picks a capability
   │                 ├─ MCP tool ──────► telemetry / manuals
   │                 └─ delegate ──────► A2A ──► Reliability Specialist
   │                                              (own prompt, own model tier)
   │
   └─ 3. OBSERVE  results fed back; repeat until the model answers or the cap hits
```

## The framework decision (the plan's open question)

The build plan named **Microsoft Agent Framework**, with Pydantic AI as a
lighter alternative. I used **neither**, and this is the trade-off to be able to
defend:

Every model call in this system goes through the gateway's `Router`, which is
where provider fallback, cost tracking, and latency-based ranking live. An agent
framework brings **its own model clients**. Adopting one means either
(a) letting it call providers directly — which bypasses the router and silently
discards the cost tracking and fallback that are this project's whole point — or
(b) writing an adapter to force the framework's client interface onto the router,
which is more code than the loop itself.

The orchestration loop is ~120 lines. The framework's value is the loop; the
cost is losing the thing the gateway exists to do. So the loop is implemented
directly on top of the existing `Router` + `MCPToolbox`, and every model call it
makes — planning included — is cost-tracked and can fail over.

**When that reasoning would flip:** if the requirement were multi-agent
conversation patterns (group chat, hand-off graphs, shared scratchpads) or
durable/resumable workflow state across process restarts, hand-rolling stops
being cheaper and a framework earns its weight.

## The one interesting decision

**Delegation is exposed to the model as just another tool.**

`consult_reliability_specialist` sits in the same flat tool list as
`get_telemetry` and `search_manuals`. The model chooses to delegate the same way
it chooses to look something up — there is no hardcoded "if the question is
hard, call the specialist" branch anywhere.

That matters because the routing decision is the *model's*, based on the tool
description, and the mechanism (an HTTP JSON-RPC call to a different agent) is
invisible to it. That invisibility is what A2A buys: the specialist could move
to another team's service on another host and only its URL would change.

## Why plan separately, when the Phase 3 loop already worked

The reactive tool loop reaches good answers by deciding one tool at a time. The
plan buys two things it cannot:

1. **It is inspectable before anything runs.** The plan is returned to the
   caller, so "what was this agent about to do?" is answerable without server
   logs — and in a real deployment that is exactly where a human approval gate
   would sit for anything irreversible.
2. **It reduces wandering on multi-step questions.** A model that has committed
   to "check telemetry, then look up the limit, then consult" is less likely to
   answer after step one.

The plan is **advisory, not a script.** The loop may deviate when an observation
invalidates a step — a plan that cannot be departed from turns a model into a
bad workflow engine. Both the plan and the steps actually taken are reported, so
deviation is visible rather than hidden.

If the planner returns unparseable output, the run **continues without a plan**
and sets `plan_malformed: true`. A degraded start is not a failed request — but
showing a plan that did not actually guide the run would be worse than showing
none.

## What makes the specialist an agent, not a tool

A tool returns data. The specialist returns **judgement**: its own model call,
its own system prompt (failure-mode analysis, not general helpfulness), and its
own — deliberately cheaper — model tier, because bounded sub-analysis should not
cost more than doing the work inline. The orchestrator knows how to ask it, not
how it reasons.

It is also routed through the same gateway `Router`, so a delegated call is
cost-tracked exactly like a direct one.

## A2A: what is and is not implemented

**Implemented** — the core interaction model:
- Agent Card discovery at `/a2a/specialist/.well-known/agent-card.json`,
  advertising identity, capabilities, and typed skills.
- JSON-RPC 2.0 `message/send`, returning a completed Task.
- The reply comes back as an **artifact**, not a status message: in A2A,
  artifacts are the durable output of the work and status messages are progress
  commentary. Delegated analysis is output.
- Errors return as JSON-RPC error objects with HTTP 200 — an HTTP error code
  would mean the *transport* failed, which is a different claim.

**Not implemented** — `message/stream`, push notifications, multi-turn task
states (`working`, `input-required`), cancellation, authenticated extended
cards. The agent card advertises `streaming: false` rather than claiming a
default it does not honour.

This speaks enough A2A for one agent to discover another and delegate a unit of
work. **It is not a compliant A2A implementation**, and the README says so.

Implemented directly rather than via `a2a-sdk` because the wire format is small
and the protocol shape — cards, skills, JSON-RPC tasks, artifacts vs messages —
is the part worth demonstrating; an SDK would hide exactly that, and could not
be verified end to end here anyway.

## Honest status

- Planning, the act/observe loop, tool execution, the step cap, and A2A
  delegation are **all real and tested**, including a genuine JSON-RPC round trip
  over HTTP into the app.
- The loop is driven by a **scripted fake model** in tests. It has not been run
  against live Claude — that needs the API key.
- The specialist is co-located in the same process. It is reached over HTTP
  through a configurable base URL specifically so that co-location is an
  accident of deployment, not an assumption in the code.

## How to run it

```bash
curl -s http://127.0.0.1:8000/a2a/specialist/.well-known/agent-card.json
```

```bash
curl -s http://127.0.0.1:8000/v1/agent -H "content-type: application/json" -d "{\"model\":\"claude-sonnet-5\",\"messages\":[{\"role\":\"user\",\"content\":\"Pump 3 is running hot. What is wrong and what should I do?\"}]}"
```

The `gateway` block reports `plan_goal`, `plan_steps`, every `tool_step` taken,
and `delegated_to_agent`.

## Tests

`python -m pytest -q` — 109 tests. Phase 4 adds plan parsing (including fenced
and prose-wrapped JSON), plan-to-executor handoff, the step cap, cost including
the planning call, the A2A wire format, and end-to-end delegation over real HTTP.
