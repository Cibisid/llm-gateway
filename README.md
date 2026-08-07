# AI Gateway & Agent Platform

A secure, OpenAI-compatible API service that routes requests across multiple LLM
providers, runs agentic tools over the Model Context Protocol, delegates to a
second agent over Agent2Agent, tracks cost per request, and **evaluates its own
output quality in CI**.

```
                    ┌──────────────────────────────────────────────┐
  client ──────────►│  POST /v1/chat/completions   (OpenAI-shaped)  │
  (any OpenAI SDK)  │  POST /v1/agent              (plan→act→observe)│
                    └───────────────────┬──────────────────────────┘
                                        │
                    ┌───────────────────▼──────────────────────────┐
                    │  security.py   bearer auth · rate limit      │
                    │                fails CLOSED · audit log      │
                    └───────────────────┬──────────────────────────┘
                                        │
                    ┌───────────────────▼──────────────────────────┐
                    │  router.py                                   │
                    │    alias → candidates → rank → try → fall back│
                    │    strategies: order | cost | latency        │
                    │    per-request cost from verified prices     │
                    └──┬──────────────┬────────────────┬───────────┘
                       │              │                │
              ┌────────▼───┐  ┌───────▼──────┐  ┌──────▼────────┐
              │ Anthropic  │  │   OpenAI     │  │ Azure OpenAI  │
              │ (verified) │  │ (unverified) │  │ (unverified)  │
              └────────────┘  └──────────────┘  └───────────────┘
                       ▲
                       │  tool calls
        ┌──────────────┴───────────────┬─────────────────────────┐
        │                              │                         │
┌───────▼────────┐          ┌──────────▼─────────┐   ┌───────────▼──────────┐
│  MCP server    │          │  A2A specialist    │   │  eval/runner.py      │
│  telemetry     │          │  own prompt        │   │  golden set          │
│  manuals (RAG) │          │  own model tier    │   │  runs in CI          │
└────────────────┘          └────────────────────┘   └──────────────────────┘
```

## Requirement → where it lives

| Requirement | Implementation | Code |
|---|---|---|
| LLM cost / performance / capability trade-offs | Router with cost, latency, and order strategies; per-request cost from verified prices | [`app/router.py`](app/router.py), [`app/pricing.py`](app/pricing.py) |
| RAG | Maintenance-manual retrieval with citable section ids | [`mcp_server/tools/manuals.py`](mcp_server/tools/manuals.py) |
| Tool calling + MCP | MCP server (mcp 2.0.0, 2026-07-28 spec); gateway acts as MCP host/client | [`mcp_server/`](mcp_server/), [`app/mcp_client.py`](app/mcp_client.py) |
| Agent orchestration | plan → act → observe, with the plan exposed before execution | [`app/orchestrator.py`](app/orchestrator.py) |
| Agent2Agent (A2A) | Agent card discovery + JSON-RPC `message/send` to a second agent | [`app/a2a.py`](app/a2a.py), [`agents/specialist_agent.py`](agents/specialist_agent.py) |
| Automated evaluation of AI outputs | Golden set: tool-call correctness, grounding, similarity, LLM-judge; non-zero exit on regression | [`eval/runner.py`](eval/runner.py) |
| Securing APIs, auth/authz, data privacy | Bearer auth (fails closed), per-key rate limiting, PII redaction, audit log | [`app/security.py`](app/security.py), [`app/logging_setup.py`](app/logging_setup.py) |
| Azure PaaS | Container Apps + Key Vault + managed identity, in Bicep | [`infra/main.bicep`](infra/main.bicep) |
| AI ethics / NIST AI RMF / EU AI Act | Control-to-clause mapping, with an explicit gaps list | [`docs/responsible-ai.md`](docs/responsible-ai.md) |

## Status — what is verified, and what is not

This section is deliberately first-class. Several capabilities are implemented
but have **never run against a live service**, and saying so is more useful than
a green checkmark.

| Capability | Status |
|---|---|
| Gateway, routing, fallback, cost tracking | **Verified** — 166 tests |
| MCP server, discovery, tool execution | **Verified** — real protocol round trips in tests |
| Tool loop, orchestrator, A2A delegation | **Verified** with a scripted model; real JSON-RPC over HTTP |
| Security: auth, rate limiting, redaction, audit | **Verified** — 166 tests |
| Offline evaluation (tools, retrieval) | **Verified** — 9 cases pass |
| Anthropic provider against the live API | **Not verified** — no API key in the build environment |
| OpenAI / Azure OpenAI providers | **Not verified** — no credentials; mocked tests only |
| Online evaluation (model behaviour) | **Never executed** — reported as skipped, never as passed |
| Azure deployment | **Never deployed** — the Bicep is a design artefact, unvalidated against ARM |

**A2A scope:** agent card discovery and `message/send` only. No streaming, push
notifications, multi-turn task state, or cancellation. This is not a compliant
A2A implementation, and the agent card advertises `streaming: false` rather than
claiming otherwise.

## Quick start

```bash
python -m venv .venv && .venv\Scripts\activate && pip install -r requirements.txt
```

```bash
copy .env.example .env
```

Fill in `ANTHROPIC_API_KEY` and generate a gateway key:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions -H "Authorization: Bearer YOUR_GATEWAY_KEY" -H "content-type: application/json" -d "{\"model\":\"auto\",\"use_mcp_tools\":true,\"messages\":[{\"role\":\"user\",\"content\":\"Is pump 3 running too hot, and what does the manual say to do?\"}]}"
```

## Tests and evaluation

```bash
python -m pytest -q
```

```bash
python -m eval.runner --offline
```

The evaluation is the part worth looking at. It **found three real retrieval
bugs that 166 passing unit tests had missed**, because every individual function
worked correctly:

- IDF scored the stopword *"do"* as highly informative (it appeared in exactly
  one section), so *"how **do** I restart a pump"* matched the section saying
  *"**Do** not restart"*.
- A single incidental word overlap counted as a match, inviting the model to
  ground an answer in an irrelevant section.
- Set-based scoring made a section *titled* "restart procedure" tie with one
  mentioning restart once in passing — and the tie broke alphabetically.

All three are now regression tests. See
[`docs/phase5-EXPLAINER.md`](docs/phase5-EXPLAINER.md).

## Design decisions worth reading

- **[Why the Provider contract normalises usage](docs/phase1-EXPLAINER.md)** —
  the seam that keeps cost tracking, evaluation, and auditing free of
  per-vendor branching.
- **[Why aliases make cost routing meaningful](docs/phase2-EXPLAINER.md)** —
  routing by cost is nearly vacuous if the client always names a model.
- **[Why `mcp` 2.0 and not the 1.x line the plan assumed](docs/phase3-EXPLAINER.md)**.
- **[Why no agent framework was adopted](docs/phase4-EXPLAINER.md)** — and the
  conditions under which that decision reverses.
- **[Why every eval case needs a decidable check](docs/phase5-EXPLAINER.md)** —
  a muted eval is worse than no eval.
- **[Why the service fails closed](docs/phase6-EXPLAINER.md)**.

Full log with reasoning: [`docs/design-decisions.md`](docs/design-decisions.md).

## Deploying to Azure

Not yet done, and the template is unvalidated. The intended flow:

```bash
az deployment group create -g rg-llm-gateway -f infra/main.bicep -p containerImage=<acr>.azurecr.io/llm-gateway:v1
```

Secrets are created out of band in Key Vault and read by the Container App
through a user-assigned managed identity — no secret in the image, the template,
or deployment history. Note that with `maxReplicas > 1` the rate limiter and
latency tracker are per-process, so the effective rate limit multiplies by the
replica count.

## Repository layout

```
app/            gateway: router, providers, orchestrator, security, MCP client
mcp_server/     MCP server and its domain tools
agents/         the A2A specialist agent
eval/           golden set, scoring, runner
infra/          Dockerfile and Bicep
tests/          166 tests, no network and no API keys required
docs/           per-phase explainers, decision log, responsible-AI mapping
```
