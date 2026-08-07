# Phase 2 — Routing intelligence

## What it does

Turns the Phase 1 "find the one provider that serves this model" lookup into a
real routing layer: **model aliases**, three **ranking strategies**, **automatic
fallback** on retryable failures, and **per-request cost** reported back to the
caller.

```
request model
   │
   ├─ alias?  "auto" ──► expand to configured concrete models
   └─ concrete? ──────► every provider that serves it (can be >1)
                          │
                    rank by strategy (order | cost | latency)
                          │
                    try in order ──► retryable failure? next candidate
                                     non-retryable?    stop immediately
                          │
                    compute cost from the model that actually ran
```

## The one interesting decision

**Model aliases are what make cost-based routing mean anything.**

If clients only ever name a concrete model, "route by cost" is nearly vacuous —
`claude-opus-5` costs what it costs, whoever serves it. The interesting decision
is *which model to use at all*, and a client cannot delegate that unless it can
express intent instead of a model name.

So the gateway accepts `"model": "auto"` (also `auto-cheap`, `auto-quality`).
The router expands the alias to its configured members, ranks them by the active
strategy, and calls the winner. Under `cost` that is the cheapest capable model;
under `order` it is whatever config says. The client's request does not change —
only the gateway's policy does. That is the argument for having a gateway.

## Fallback: the distinction that matters

A failed call is not automatically worth retrying elsewhere.

- **Retryable** (429, 5xx, connection error) → advance to the next candidate.
  The request was fine; the provider was not.
- **Non-retryable** (400, 401) → stop immediately. A malformed request will fail
  identically on every provider, so falling back buys a second guaranteed
  failure plus its latency and its cost.

Crucially, that classification is made **by the adapter**, not the router
(`ProviderError.retryable`). Only the adapter knows what its vendor's exception
types mean. The router just reads a boolean — which is why it still contains no
vendor-specific code.

## Cost: the deliberate gap

`app/pricing.py` contains only prices actually verified against a vendor's
published pricing, each carrying its source and the date checked. Anthropic's
four models are priced. **OpenAI and Azure models are deliberately absent**, so
they report `cost_usd: null`.

That is a choice, not an omission. A confidently wrong cost figure is worse than
an admitted unknown, because nobody re-checks a number that looks plausible. The
cost ranking respects this too: an unpriced model sorts **last**, never first —
"unknown" must never be mistaken for "free".

Money is computed in `Decimal`, not `float`, and rounded to 8dp. Two reasons:
these values get summed across many requests, and at 4dp a cheap Haiku call
rounds to exactly zero.

## Latency tracking

An in-memory EWMA per provider (α = 0.3), fed by every call — **including failed
ones**, since a provider that is timing out is genuinely slow and the ranking
should learn that. Providers with no observation yet sort *first* under the
latency strategy, deliberately: otherwise a provider that was never sampled
would never be sampled, and the ranking would freeze on whichever one happened
to be measured first.

Honest limitation: this is per-process. Across replicas each instance forms its
own estimate. Making it shared would mean Redis or similar — a real design
decision, out of scope here, and noted in the code rather than pretended away.

## Also in this phase

- **Azure OpenAI adapter** (mocked tests only — no endpoint available). Its one
  real quirk: Azure addresses a *deployment name* you chose, not a public model
  id, so its servable model id comes from configuration. Exactly the kind of
  vendor weirdness the Provider contract exists to absorb.
- Registry requires **all four** Azure settings before registering, and logs
  loudly on a partial config — that pattern is a typo, not an opt-out.

## How to see it work

```bash
$env:ROUTING_STRATEGY = "cost"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions -H "content-type: application/json" -d "{\"model\":\"auto\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}"
```

The `gateway` block reports which concrete model was chosen, what it cost,
which strategy chose it, and every candidate attempted on the way:

```json
"gateway": {
  "provider": "anthropic",
  "upstream_model": "claude-haiku-4-5",
  "routing_strategy": "cost",
  "cost_usd": 0.00003400,
  "fallback_occurred": false,
  "attempts": [{"candidate": "anthropic/claude-haiku-4-5", "ok": true}]
}
```

`GET /healthz` also reports the active strategy and the latency the router is
currently steering on.

## Tests

`python -m pytest -q` — 60 tests, still no network and no keys. Phase 2 adds
coverage for alias expansion, all three rankings, unpriced-sorts-last, fallback
on retryable / no-fallback on non-retryable, cost arithmetic, and the EWMA.
