# AI Gateway & Agent Platform

[![CI](https://github.com/Cibisid/llm-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/Cibisid/llm-gateway/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-167%20passing-brightgreen)](tests/)
[![Eval](https://img.shields.io/badge/eval-23%2F23%20vs%20live%20model-brightgreen)](eval/)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](requirements.txt)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

A secure, OpenAI-compatible API service that routes requests across multiple LLM
providers, runs agentic tools over the Model Context Protocol, delegates to a
second agent over Agent2Agent, tracks cost per request, and **evaluates its own
output quality in CI**.

It answers questions using **real live public data** — UK river levels, aviation
weather, and ECB exchange rates — alongside private mock enterprise data, which
is the realistic enterprise shape: your own systems plus outside feeds.

## Try it in 60 seconds — no API key, no cost

```bash
git clone https://github.com/Cibisid/llm-gateway.git
```

```bash
cd llm-gateway && python -m venv .venv && pip install -r requirements.txt && python demo.py
```

Activate the venv first if your shell needs it — `.venv\Scripts\activate` on
Windows, `source .venv/bin/activate` elsewhere.

`demo.py` walks the full request path with the **model stubbed**, so it runs
free, offline, and without credentials. Same for the deterministic half of the
evaluation suite:

```bash
python -m pytest -q
```

```bash
python -m eval.runner --offline
```

Got an Anthropic key? `python live_demo.py "what's the river level at York?"`
runs the whole thing against a real model and real live data for a few pence.

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
│  telemetry·RAG │          │  own prompt        │   │  golden set          │
│  + LIVE feeds  │          │  own model tier    │   │  runs in CI          │
└───────┬────────┘          └────────────────────┘   └──────────────────────┘
        │
        │  no API key needed
        ▼
  Environment Agency (rivers) · NOAA (aviation weather) · ECB (FX rates)
```

## Requirement → where it lives

| Requirement | Implementation | Code |
|---|---|---|
| LLM cost / performance / capability trade-offs | Router with cost, latency, and order strategies; per-request cost from verified prices | [`app/router.py`](app/router.py), [`app/pricing.py`](app/pricing.py) |
| RAG | Maintenance-manual retrieval with citable section ids | [`mcp_server/tools/manuals.py`](mcp_server/tools/manuals.py) |
| Tool calling + MCP | MCP server (mcp 2.0.0, 2026-07-28 spec); gateway acts as MCP host/client | [`mcp_server/`](mcp_server/), [`app/mcp_client.py`](app/mcp_client.py) |
| Agent orchestration | plan → act → observe, with the plan exposed before execution | [`app/orchestrator.py`](app/orchestrator.py) |
| Agent2Agent (A2A) | Agent card discovery + JSON-RPC `message/send` to a second agent | [`app/a2a.py`](app/a2a.py), [`agents/specialist_agent.py`](agents/specialist_agent.py) |
| Grounding in real external data | Three live public feeds with failures returned as advice, not exceptions | [`mcp_server/tools/live.py`](mcp_server/tools/live.py) |
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
| Gateway, routing, fallback, cost tracking | **Verified** — 167 tests |
| MCP server, discovery, tool execution | **Verified** — real protocol round trips in tests |
| Security: auth, rate limiting, redaction, audit | **Verified** — 167 tests |
| Offline evaluation (tools, retrieval) | **Verified** — 9 cases pass |
| **Anthropic provider against the live API** | **Verified** — real calls to `api.anthropic.com` |
| **Tool loop, orchestrator, A2A delegation** | **Verified against live Claude**, not just a scripted model |
| **Live public data tools** (rivers, aviation weather, FX) | **Verified** — real calls to the Environment Agency, NOAA, and ECB |
| **Full evaluation, all 23 cases** | **Verified** — 23 passed, 0 failed, 0 skipped, ~$0.43 |
| **End-to-end over real HTTP** | **Verified** — 26/26 checks against a running uvicorn server (`verify.py`) |
| OpenAI / Azure OpenAI providers | **Not verified** — no credentials; mocked tests only |
| Azure deployment | **Never deployed** — the Bicep is a design artefact, unvalidated against ARM |
| Local Docker build | **Not verified locally** — Docker Desktop's Linux engine would not start; CI builds the image |

**A2A scope:** agent card discovery and `message/send` only. No streaming, push
notifications, multi-turn task state, or cancellation. This is not a compliant
A2A implementation, and the agent card advertises `streaming: false` rather than
claiming otherwise.

## Live data — real feeds, no API key required

The agent answers from three live public sources, all free and unauthenticated,
alongside the private mock enterprise tools. That combination is deliberate:
real systems answer questions using in-house data *and* outside feeds.

| Tools | Source | What to know |
|---|---|---|
| `find_river_stations`, `get_river_level`, `get_flood_warnings` | UK Environment Agency | ~4,500 stations, 15-minute updates. Levels are **mASD** — relative to a per-station datum, **not** a depth of water |
| `get_airport_weather`, `get_airport_forecast` | NOAA Aviation Weather | **ICAO** codes (`EGLL`), not IATA (`LHR`). Returns raw METAR so the model decodes it |
| `get_exchange_rates`, `convert_currency`, `get_historical_rate` | ECB via frankfurter.dev | **Daily reference** rates, not live market rates. A non-EUR base is a derived cross-rate |

Those caveats are in the tool descriptions, not just this table — the model is
told what the numbers mean so it cannot confidently misreport them. Getting the
ECB attribution right was a bug the evaluation caught, not one anticipated.

Shared plumbing lives in [`mcp_server/tools/live.py`](mcp_server/tools/live.py):
timeouts, TTL caching, and **every failure returned as data with actionable
advice rather than raised**. A 404 is distinguished from an outage, because the
model should react differently to "that station does not exist" than to "the
service is down".

## Running the full gateway

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

## Use it from the OpenAI SDK

The gateway speaks the OpenAI wire format, so the official `openai` package
works against it unchanged — only `base_url` and `api_key` differ:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="YOUR_GATEWAY_KEY")

resp = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "In one sentence: what is a centrifugal pump?"}],
)
print(resp.choices[0].message.content)
```

That returns a real `ChatCompletion` object — `resp.model`, `resp.usage`,
`resp.choices[0].finish_reason` all populated as an OpenAI client expects:

```
A centrifugal pump uses a rotating impeller to accelerate fluid outward from
the center, converting rotational energy into pressure and flow.
```

The difference is what rides along with it. Every response carries a `gateway`
block recording what actually happened, which no OpenAI-compatible service
gives you by default:

```json
{
  "provider": "anthropic",
  "upstream_model": "claude-haiku-4-5-20251001",
  "latency_ms": 1538,
  "routing_strategy": "order",
  "cost_usd": 0.000185,
  "fallback_occurred": false,
  "attempts": [{"candidate": "anthropic/claude-haiku-4-5", "ok": true, "error": null}]
}
```

You asked for `auto`; the router picked Haiku, served it in 1.5 s, and priced
the call at $0.000185. An unrecognised client ignores the extra block; a client
that cares reads its own cost off every response.

### The agent, through the same SDK

Set one extra field and the model gets the MCP tools — including the live
public data sources:

```python
resp = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "What is the current river level at York?"}],
    extra_body={"use_mcp_tools": True},
)
```

A real answer from a real run, against the live Environment Agency feed:

> The current river levels at York are:
>
> **York Foss Barrier (River Ouse):**
> - Current level: **5.087 mASD** (recorded at 23:15 on 10 Aug 2026)
> - Typical range: 5.052 to 7.9 mASD
> - Status: Within normal

The `gateway` block then also carries `tool_steps` — every tool the model
called, with its arguments and what came back, so the answer can be audited
rather than trusted:

```json
"tool_steps": [
  {"iteration": 1, "tool": "find_river_stations", "arguments": {"place": "York", "limit": 5}},
  {"iteration": 2, "tool": "get_river_level",     "arguments": {"station_id": "L2404"}},
  {"iteration": 2, "tool": "get_river_level",     "arguments": {"station_id": "L2406"}}
]
```

Note the two calls sharing iteration 2: the model found several York stations
and fetched them in parallel within one turn, rather than serialising two round
trips. That request cost $0.0111 in total.

## Tests and evaluation

```bash
python -m pytest -q
```

```bash
python -m eval.runner
```

**Result: 23 passed, 0 failed, 0 skipped — ~$0.43 in model spend.** Including the
two that matter most: asked about a pump that does not exist, the agent invented
no reading; asked for a procedure with no manual section, it refused to invent
one. `--offline` runs the 9 deterministic cases with no key at all.

The evaluation is the part worth looking at. It **found five real bugs that 167
passing unit tests had missed**, because every individual function worked
correctly:

- IDF scored the stopword *"do"* as highly informative (it appeared in exactly
  one section), so *"how **do** I restart a pump"* matched the section saying
  *"**Do** not restart"*.
- A single incidental word overlap counted as a match, inviting the model to
  ground an answer in an irrelevant section.
- Set-based scoring made a section *titled* "restart procedure" tie with one
  mentioning restart once in passing — and the tie broke alphabetically.
- **The LLM judge failed a correct answer** for "using a future date (10 Aug
  2026)" when it genuinely *was* 10 Aug 2026 — the judge had inferred today's
  date from its training data. Fixed by injecting the real date into the judge
  prompt. A judge that is confidently wrong about the world will fail correct
  answers, and you will not notice unless you read the verdicts.
- The judge then caught a **genuine factual inaccuracy**: the ECB publishes
  *euro* reference rates, so a GBP→USD figure is a derived cross-rate, not a
  published one. The tool's attribution was wrong and is now fixed.

All five are now regression tests. See
[`docs/phase5-EXPLAINER.md`](docs/phase5-EXPLAINER.md).

### Guardrails that keep the eval honest

An evaluation suite that quietly passes is worse than none, so the harness is
defended by tests of its own:

- Every online case must carry at least one **decidable** check — a tool call, an
  argument, a required or forbidden substring. A meta-test enforces it.
- Exactly one documented exemption exists, and a second test caps how many cases
  may use it.
- **Skips report as SKIP, never PASS.**
- The judge **fails** on an unreachable or unparseable verdict. It never passes
  by default.
- Live-data cases assert on the *shape* of a correct answer — right tool, station
  named, rate dated, refusal to invent — never on values that change every 15
  minutes.

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
- **[What the container and Bicep do, and what is unvalidated](docs/phase7-EXPLAINER.md)**.

Full log with reasoning: [`docs/design-decisions.md`](docs/design-decisions.md).

### Known limitations, stated plainly

- The **rate limiter and latency tracker are per-process**. Running N replicas
  multiplies the effective rate limit by N. Redis is the fix; it is documented
  rather than pretended away.
- **OpenAI and Azure OpenAI adapters are unproven against live services** — the
  tests are mocked because there are no credentials.
- **OpenAI and Azure models are deliberately unpriced** and report
  `cost_usd: null`. Unpriced sorts *last* under cost ranking, so "unknown price"
  is never silently treated as "free".
- The **Bicep template has never been applied.** It is a design artefact.

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
mcp_server/     MCP server, private mock tools, and the live public-data tools
agents/         the A2A specialist agent
eval/           golden set, scoring, runner
infra/          Dockerfile and Bicep
tests/          167 tests, no network and no API keys required
docs/           explainers, decision log, responsible-AI mapping

demo.py         walkthrough of the request path, model STUBBED — free, no key
live_demo.py    the same path against a real model and real live data (~15p)
verify.py       26 end-to-end checks against a real running uvicorn server
```

## The four files worth reading

If you only read four, read these — they carry the design:

| File | Why |
|---|---|
| [`app/providers/base.py`](app/providers/base.py) | The Provider contract. Usage is normalised here, which is the seam that keeps cost tracking, evaluation, and auditing free of per-vendor branching |
| [`app/router.py`](app/router.py) | Alias → candidates → rank → try → fall back. Never imports a vendor SDK and never branches on provider name |
| [`app/orchestrator.py`](app/orchestrator.py) | plan → act → observe, with the plan exposed before it executes |
| [`eval/runner.py`](eval/runner.py) | The golden set and its scoring — the part that caught five real bugs |
