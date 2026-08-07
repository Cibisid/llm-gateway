# Design decisions

A running log. Every real decision gets a line and a reason. Newest phase at
the bottom.

## Phase 0 — Setup (2026-08-06)

- **Repo lives at `GITHUB AUTOMATION/llm-gateway`, a sibling of the existing
  projects.** The session's original working directory was an unrelated empty
  folder; keeping projects separate avoids a misleading repo name.
- **GitHub repo created private initially.** It is portfolio evidence, but
  publishing half-built work costs more than flipping the flag later.
- **Python 3.14.4 local, but `requires-python = >=3.11`.** Every pinned
  dependency advertises `>=3.10`; 3.11 is the floor the plan specifies and
  keeps the container image on a well-supported base.
- **Pinned exact versions rather than ranges.** The plan requires
  reproducibility, and it makes "what did this actually run against" an
  answerable question in an interview.
- **`mcp` pinned deliberately, decision deferred to Phase 3.** PyPI now
  resolves `mcp` to **2.0.0**; the build plan assumed the 1.x line and
  suggested `mcp>=1.28,<2`. This is a real fork in the road, not a typo —
  it will be decided in Phase 3 after reading the current SDK docs, and the
  reason recorded here.
- **Only `ANTHROPIC_API_KEY` is available.** Consequence for honesty: the
  Anthropic path is verified end to end; OpenAI and Azure OpenAI adapters are
  implemented against the same contract and covered by mocked tests only.
  They are labelled "unverified against the live API" everywhere until a key
  exists.
- **Azure CLI not installed.** Blocks nothing before Phase 7; noted so it is
  not discovered late.

## Phase 1 — Core gateway (2026-08-06)

- **Adapters return primitives, not a finished OpenAI envelope.** Envelope
  assembly (id, timestamp, `object`) is identical for every provider, so doing
  it once in `main.py` beats duplicating it per adapter. Adapters translate;
  nothing else.
- **Usage normalised into a neutral `Usage` dataclass at the adapter boundary.**
  Anthropic says `input_tokens`/`output_tokens`, OpenAI says
  `prompt_tokens`/`completion_tokens`. Collapsing that here is what keeps
  Phase 2's cost tracking, Phase 5's eval, and Phase 6's audit log free of
  per-vendor branching. This is the load-bearing decision of the phase.
- **`ProviderError` carries a `retryable` flag, classified by the adapter.**
  Only the adapter knows what its vendor's exception types mean. Phase 2's
  fallback keys on this flag — retrying a 400 on another provider is futile,
  retrying a 429 is the entire point.
- **`select_candidates()` returns a list even though Phase 1 never uses more
  than one entry.** Makes Phase 2 a change of sort order inside one function
  rather than a rewrite of every caller.
- **Anthropic adapter deliberately drops `temperature`.** Current Claude models
  return a 400 for `temperature`/`top_p`/`top_k`. The public schema still
  accepts `temperature` because real OpenAI clients always send one, so the
  adapter must discard it. The OpenAI adapter forwards it. Sharpest concrete
  justification for having an adapter layer at all.
- **Anthropic `max_tokens` defaulted to 4096 when the client omits it.**
  Mandatory upstream, optional in the OpenAI schema; without a default, valid
  requests would fail on a technicality.
- **Text extracted by filtering content blocks on `type == "text"`, not
  `content[0].text`.** With thinking enabled the first block is often not the
  text block, so index-zero access is a latent bug.
- **`stream: true` and `tools` return 400 rather than being ignored.** An
  accepted-and-ignored flag is a lie in the shape of a feature.
- **Unknown request fields rejected (`extra="forbid"`) → 422.** A client sending
  an option we don't honour should learn immediately.
- **Unknown model → 404; upstream failure → 502.** Routing fails before any
  network call, so it is not a bad-gateway condition.
- **Providers registered only when their credential exists.** With only an
  Anthropic key, a GPT request returns "no provider configured" instead of
  failing deep inside an SDK on auth.
- **`/healthz` lists actually-reachable models** rather than a bare `ok`, so a
  deployment with no keys mounted is visible from the probe.
- **Router never imports a vendor SDK or branches on `provider.name`.** The
  moment it does, the adapter contract has failed.
