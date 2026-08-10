# Project: AI Gateway & Agent Platform

## What this is
A secure, OpenAI-compatible FastAPI service that routes across multiple LLM
providers, runs agentic tools over MCP, delegates to a second agent over A2A,
tracks cost per request, and evaluates its own output quality in CI.

Portfolio project for an AVEVA "AI Software Engineer, Core AI Services" role.
Owner: Cibisid. Repo: https://github.com/Cibisid/llm-gateway (private).

---

# CURRENT STATE — read this first

**All 8 phases are BUILT, TESTED, and PUSHED. Plus three live data sources.**
Do not rebuild anything. Ask what the owner wants before changing architecture.

## Verified working (evidence, not assumption)
- **167 unit tests pass** — no network, no keys needed
- **23/23 eval cases pass** against live Claude and live data, exit 0, ~$0.43
- **26/26 end-to-end checks** against a real uvicorn server over real HTTP
- Live Anthropic calls work; key is in `.env` and the account has credit

## NOT verified — say so, never imply otherwise
- **OpenAI / Azure OpenAI adapters** — implemented, mocked tests only, no credentials
- **Azure deployment** — `infra/main.bicep` has NEVER been applied, unvalidated against ARM
- **Local Docker build** — Docker Desktop's Linux engine would not start here; CI covers it

---

# Environment facts (verified, will save you time)

- Shell is **Windows PowerShell 5.1**. `&&` IS A SYNTAX ERROR. Use `;` or separate commands.
- Python: `.venv\Scripts\python.exe` (3.14 locally; the container pins 3.12)
- Heredocs (`<<'EOF'`) do not work in PowerShell. For multi-line git messages,
  write a file and use `git commit -F <file>`.
- `Set-Content -Encoding utf8` writes a BOM that breaks `pytest.ini` etc. Use the Write tool.
- Long `git commit -m` strings containing quotes get mangled — use `-F`.

## Commands (from the repo root, venv active)
```
python -m pytest -q          # 167 tests, free, ~15s
python -m eval.runner        # 23 cases vs live model+data, ~35p, ~90s
python -m eval.runner --offline   # 9 deterministic cases, free, no key
python verify.py             # 26 checks vs a real running server
python demo.py               # walkthrough, model STUBBED, free
python live_demo.py          # real model + real live data, ~15p
python live_demo.py "your question here"
```

---

# Hard rules
- **Describe only what actually runs.** Never label a stub, or an unverified
  path, as working. The honesty about gaps is a feature of this project.
- No secret ever enters git. `.env` is gitignored; `.env.example` holds names only.
- Every non-obvious block gets a one-line "why" comment.
- Append real decisions to `docs/design-decisions.md`.
- Pin exact dependency versions.

# Conventions
- Python 3.11+, FastAPI, Pydantic v2, async where it matters.
- Providers implement the contract in `app/providers/base.py`. Adding a provider
  must not require touching `app/router.py` beyond registration.
- The router must NEVER import a vendor SDK or branch on `provider.name`.
- Tests live in `tests/`, run with no network and no API keys.

---

# Decisions already made — do not relitigate without asking

- **`mcp==2.0.0`**, not the 1.x line the original build plan assumed. 2.0 is the
  current stable line implementing the 2026-07-28 spec; 1.x is maintenance-only.
  v2 uses `input_schema`, not `inputSchema`.
- **No agent framework** (not Microsoft Agent Framework, not Pydantic AI). Every
  model call goes through the Router, where fallback and cost tracking live; a
  framework brings its own model clients and would bypass that. Reverses only if
  multi-agent conversation patterns or durable workflow state are needed.
- **A2A implemented directly**, not via `a2a-sdk`. Scope is card discovery +
  `message/send` ONLY. No streaming, push notifications, or task state. The agent
  card advertises `streaming: false`. It is NOT a compliant A2A implementation.
- **Only verified prices in `app/pricing.py`.** OpenAI/Azure models are
  deliberately unpriced and report `cost_usd: null`. Unpriced sorts LAST under
  cost ranking, so "unknown" is never mistaken for "free".
- **Service fails CLOSED** — no `GATEWAY_API_KEYS` means 503 on everything.
- **Rate limiter and latency tracker are per-process.** N replicas = N x the
  limit. Documented, not fixed. Redis is the fix if scaling.

---

# The eval harness is the project's differentiator

`eval/runner.py` + `eval/golden_set.yaml`. It has caught **five real bugs** that
unit tests missed:

1. IDF scored the stopword "do" as informative (df=1), so "how **do** I restart
   a pump" matched the section saying "**Do** not restart" → stopword filtering
2. One incidental term overlap counted as a match → minimum query-coverage threshold
3. A section *titled* "restart procedure" tied with a passing mention, tie broke
   alphabetically → sub-linear TF + title weight
4. **The LLM judge failed a correct answer for "using a future date (10 Aug 2026)"
   when it WAS 10 Aug 2026** — the judge inferred today from its training data.
   Fixed by injecting the real date into the judge prompt.
5. The judge then caught a genuine inaccuracy: the ECB publishes *euro* reference
   rates, so GBP/USD is a derived cross-rate. The tool's attribution was fixed.

## Rules that keep it trustworthy
- Every online case must carry at least one **decidable** check (tool call,
  argument, required/forbidden substring). A meta-test enforces this.
- One documented exemption exists (`decidable_exempt`), and a second test caps
  how many cases may use it.
- **Skips are reported as SKIP, never PASS.**
- The judge FAILS on unreachable or unparseable verdicts — never passes.
- Live-data cases assert on the SHAPE of a correct answer (right tool, station
  named, rate dated, refusal to invent), never on values that change every 15 min.

---

# Live data sources (all free, no API key)

| Tool group | Source | Notes |
|---|---|---|
| `find_river_stations`, `get_river_level`, `get_flood_warnings` | Environment Agency | ~4,500 stations, 15-min updates. Levels are mASD — relative to a per-station datum, NOT a depth of water |
| `get_airport_weather`, `get_airport_forecast` | NOAA Aviation Weather | ICAO codes (EGLL), not IATA (LHR). Returns raw METAR so the model can decode it |
| `get_exchange_rates`, `convert_currency`, `get_historical_rate` | ECB via frankfurter.dev | Daily reference rates, NOT live market rates. Non-EUR bases are derived cross-rates |

Shared plumbing in `mcp_server/tools/live.py`: timeouts, TTL caching, and every
failure returned as data with actionable advice rather than raised. 404 is
distinguished from an outage — different advice for the model.

Private mock tools (`get_telemetry`, `search_manuals`, `list_assets`) are kept
alongside deliberately: private enterprise data + live public feeds is the point.

---

# The four files the owner must be able to explain cold
`app/providers/base.py`, `app/router.py`, `app/orchestrator.py`, `eval/runner.py`.
Keep these especially clear and well-commented. **This is still outstanding** —
the owner built everything in one pass and skipped the per-phase review gates.

# Likely next steps
1. Walk the owner through those four files with likely interview questions
2. A small web UI so demo audiences can type questions instead of running Python
3. Verify the Docker build once Docker Desktop starts
4. Optionally deploy to Azure (billable — get explicit approval first)
