# Phase 6 — Security

## What it does

Every model-invoking endpoint now requires `Authorization: Bearer <key>`, is
rate limited per key, and writes a structured audit record. Prompt content is
not logged unless explicitly enabled.

| Endpoint | Auth | Why |
|---|---|---|
| `POST /v1/chat/completions` | **required** | spends money |
| `POST /v1/agent` | **required** | spends money |
| `POST /a2a/specialist` | **required** | spends money — A2A is not a back door |
| `GET /healthz` | open | a liveness probe needing a credential fails during a credential outage |
| `GET /a2a/.../agent-card.json` | open | A2A discovery documents are meant to be publicly readable; it carries no secrets |

## The threat model, stated honestly

**Defends against:** anonymous use of the endpoint (the primary risk of an LLM
gateway is *financial* — anyone who can reach the port spends your provider
credits); one client exhausting the budget for everyone; prompts leaking into
log aggregators; timing attacks on key comparison.

**Does not defend against:** a stolen key — there is no rotation, expiry, or
revocation here, keys are static config and a real deployment needs a key
service. A distributed attacker — the rate limiter is per-process, so three
replicas permit three times the limit. Prompt injection from tool output — that
is mitigated in the tool layer, and mitigation is not prevention.

Writing down what a control *doesn't* do is the part that makes the rest
credible.

## The one interesting decision

**The service fails closed.**

With `GATEWAY_API_KEYS` unset, every request is refused with 503 — the gateway
does not fall back to running unauthenticated. An auth layer that disables
itself when misconfigured is worse than no auth layer, because the deployment
*looks* protected and nobody checks again.

The tests assert this, and they assert the negative that matters: on a rejected
request, **the provider is never called**. Auth that runs after the expensive
part is decoration.

## Details worth defending

**Constant-time key comparison.** `secrets.compare_digest`, iterating over every
candidate with no early exit on match. A plain `in` or `==` short-circuits at
the first differing byte, so response timing leaks the key prefix and an
attacker recovers it byte by byte.

**Keys are logged by fingerprint, never by value.** SHA-256 truncated to 12 hex
chars — enough to correlate a client's requests, useless for authenticating.
Failed attempts are logged by fingerprint *of the presented value*, so repeated
probing is visible without ever writing a credential to disk.

**Content logging is off by default.** Prompts are the most sensitive thing
flowing through this service. Logging them by default pushes customer data and
pasted credentials into whatever log sink the deployment uses, with whatever
retention it has. When enabled, content is **redacted then truncated** — in that
order, because truncating first could split a pattern and leave a partial
credential in the log. There is exactly one function in the codebase where
content can enter the audit log, so a second path can't be added without the
check.

**Redaction is labelled best-effort.** It is regex: it masks emails, cards, IPs,
phone numbers, and API-key-shaped strings, and it will not catch a name or an
unusual account format. There is a test asserting that limitation so nobody
mistakes it for a guarantee. It is defence in depth *under* "don't log content",
which is the control that actually protects the data.

Pattern order matters and is commented: the loose phone pattern matches
dot-separated digit runs, so an IP would be masked as `[PHONE]` if it ran first
— still redacted, but a misleading label in an audit trail is its own bug.

**Failures are audited too.** An audit trail recording only successes cannot
answer "was this ever refused?" — which is exactly what a compliance review
asks.

**The delegated A2A call forwards the caller's credential** rather than the
orchestrator holding a privileged internal key. Two reasons: the specialist is a
model-invoking endpoint that must not be reachable unauthenticated, and
forwarding preserves attribution — the delegated call appears in the audit log
under the *original* caller's key, not a service identity that hides who spent
the money.

**The audit logger does not propagate.** Its stream must not be reformatted,
filtered, or duplicated by the application logger's configuration. (This is why
pytest's `caplog` can't see audit records, and why the tests attach a handler to
the real logger instead — testing the actual behaviour rather than working
around it.)

## What is verified

- All 167 tests pass, including: unauthenticated and wrong-key requests never
  reach a provider; malformed `Authorization` headers in four shapes are
  rejected; the service 503s when unconfigured; rate limiting throttles the
  right request and no further provider calls happen; keys never appear in an
  audit record; content is absent unless enabled; redaction precedes truncation.
- Repo scan: `.env` is untracked, no secret-shaped strings in any tracked file,
  and all three model-invoking endpoints carry the auth dependency.

## How to run it

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Put that in `.env` as `GATEWAY_API_KEYS`, then:

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions -H "Authorization: Bearer YOUR_KEY" -H "content-type: application/json" -d "{\"model\":\"claude-haiku-4-5\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}"
```

Omit the header and you get 401. The audit line lands on stdout as JSON.
