# Phase 1 — Core gateway

## What it does

Exposes `POST /v1/chat/completions` with the same request and response shape as
OpenAI's API, so any existing OpenAI client library can point at this service by
changing only its base URL. Behind that endpoint, a router resolves the requested
model to a provider adapter, and the adapter translates to and from that vendor's
SDK.

```
client ──► POST /v1/chat/completions
             │
             ├─ schemas.py      validate (reject unknown fields, stream, tools)
             ├─ router.py       resolve model ──► ordered provider candidates
             ├─ providers/*.py  translate ──► vendor SDK ──► translate back
             └─ main.py         assemble the OpenAI envelope + gateway metadata
```

## The one interesting decision

**Usage is normalised at the adapter boundary, and adapters return primitives
rather than a finished response envelope.**

Anthropic reports `input_tokens` / `output_tokens`. OpenAI reports
`prompt_tokens` / `completion_tokens`. If that difference leaked past the
adapter, every downstream consumer — cost tracking, the audit log, the eval
harness — would need its own `if provider == "anthropic"` branch. Collapsing it
into a single `Usage` dataclass in `app/providers/base.py` is what lets Phase 2
compute cost without knowing which vendor served the request.

The corollary: adapters return `text`, `usage`, `finish_reason`, and nothing
else. Envelope assembly (id, timestamp, `object: "chat.completion"`) is identical
for every provider, so it happens once in `main.py`. An adapter's only job is
translation.

## The three translation problems this phase actually solved

These are not hypothetical — each one breaks every request from a standard
OpenAI client if you get it wrong.

1. **System prompt placement.** OpenAI puts the system prompt in the messages
   array as `role: "system"`. Anthropic takes it as a separate top-level
   `system=` argument and rejects a "system" role inside `messages`. The adapter
   hoists them out, joining multiple system messages rather than dropping any.

2. **`max_tokens` is mandatory on Anthropic, optional on OpenAI.** A perfectly
   valid OpenAI-shaped request that omits it would 400. The adapter supplies a
   default.

3. **Current Claude models reject sampling parameters.** `temperature`, `top_p`,
   and `top_k` return a 400. The gateway's public schema still accepts
   `temperature`, because real OpenAI clients always send one — so the Anthropic
   adapter must deliberately *drop* it, while the OpenAI adapter forwards it.
   This is the sharpest example of why the adapter layer exists at all.

## Honest status

- **Anthropic adapter** — implemented. Verified end to end only once a real
  `ANTHROPIC_API_KEY` is present in `.env`; until then it is exercised by
  mocked tests only.
- **OpenAI adapter** — implemented against the same contract, covered by mocked
  tests, **never run against the live OpenAI API** (no key available). Do not
  describe it as verified.
- **No authentication.** Anyone who can reach the port can spend provider
  credits. Auth, rate limiting, and audit logging arrive in Phase 6. Bind to
  localhost until then.
- `stream: true` and `tools` are **rejected with a 400**, not accepted and
  ignored.

## How to run it

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

```bash
curl -s http://127.0.0.1:8000/healthz
```

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions -H "content-type: application/json" -d "{\"model\":\"claude-haiku-4-5\",\"messages\":[{\"role\":\"user\",\"content\":\"Say hello in five words.\"}]}"
```

The response carries a `gateway` block reporting which provider served the
request, which upstream model answered, and how long it took.

## Tests

```bash
python -m pytest -q
```

40 tests, no network access and no API keys required. They cover the three
translation problems above, stop-reason mapping, error classification
(retryable vs not — Phase 2's fallback depends on that flag), and the HTTP
status contract (404 for an unknown model, 502 for an upstream failure).
