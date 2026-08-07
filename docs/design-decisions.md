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

## Phase 2 — Routing intelligence (2026-08-06)

- **Model aliases (`auto`, `auto-cheap`, `auto-quality`).** Cost-based routing
  is close to vacuous when the client names a concrete model — the interesting
  choice is *which model at all*, and the client can only delegate that if it
  can express intent. Aliases are what make the routing layer worth having.
- **`Candidate` = (provider, model) pair, not just a provider.** An alias
  resolves to several models on one provider; a concrete model can resolve to
  several providers (gpt-4o via OpenAI direct and via Azure). One type covers
  both, so `route()` never branches on which kind of request it was.
- **Ranking is the only thing a strategy changes.** `_rank()` is the single
  seam; execution, fallback, and cost are strategy-independent. Adding a
  fourth strategy is a new branch there and nothing else.
- **Unpriced models sort LAST under the cost strategy.** "Unknown price" must
  never be mistaken for "free", or the cheapest-looking option becomes the one
  we simply failed to price.
- **Only verified prices are in the table, each with source and date.** OpenAI
  and Azure models are deliberately absent and report `cost_usd: null`. A
  confidently wrong cost is worse than an admitted unknown, because nobody
  re-checks a plausible-looking number.
- **Money computed in `Decimal`, rounded to 8dp.** Float error accumulates
  exactly where a billing dashboard shows it; 4dp would report most Haiku calls
  as costing zero.
- **Cost is computed from the concrete model that ran, not the requested one.**
  An alias has no price.
- **Fallback only on retryable errors.** A 400 fails identically everywhere, so
  retrying buys a second guaranteed failure plus its latency and cost. The
  retryable/non-retryable call is the adapter's, not the router's.
- **Failed attempts feed the latency tracker too.** A provider that is timing
  out is slow; the ranking should learn that rather than only sampling successes.
- **Unmeasured providers rank FIRST under the latency strategy.** Otherwise a
  provider that was never sampled never gets sampled and the ranking freezes on
  whichever one was measured first.
- **Latency EWMA is per-process and in-memory.** Across replicas each instance
  forms its own estimate. Sharing it would mean Redis or similar — a genuine
  design decision deliberately left out of scope and documented in the code
  rather than papered over.
- **Every attempt is reported in the response**, not just the winner. A fallback
  that is invisible cannot be debugged when someone asks why a request was slow.
- **Azure OpenAI advertises its deployment name as the model id.** Azure routes
  on a customer-chosen deployment name rather than the public model id, so the
  servable id comes from settings. This is the clearest example of the Provider
  contract absorbing vendor weirdness the router never sees.
- **Azure requires all four settings before registering**, and a partial config
  logs a warning — that combination is a typo, not a deliberate opt-out.

## Phase 3 — Tool calling + MCP (2026-08-07)

- **`mcp` pinned to 2.0.0, resolving the fork flagged in Phase 0.** The build
  plan assumed 1.x and suggested `<2`, but 2.0.0 is now the stable line, it
  implements the 2026-07-28 spec the plan names as current, and 1.x is
  maintenance-only. Building new work on a maintenance branch would be the
  wrong call. The v2 API was verified by introspection and a live round trip
  before any code was written against it — v2 renamed `inputSchema` to
  `input_schema`, which a from-memory implementation would have got wrong.
- **The Provider contract gained a neutral tool vocabulary** (`ToolSpec`,
  `ToolCall`, `ToolOutcome`, `Turn`). The two vendors model tool calling
  incompatibly — Anthropic returns parsed arguments and puts results in
  `tool_result` blocks on a *user* message; OpenAI returns a JSON *string* and
  uses a dedicated `role: "tool"` message per result. Letting either shape leak
  upward would put vendor-shaped dicts in the orchestrator.
- **`extra_turns` is separate from `request.messages`.** The tool conversation
  is the gateway's, not the client's; keeping them apart means a plain call
  needs neither argument and Phase 6's audit log can record the client's
  request unchanged.
- **Tool results are untrusted input.** They return inside `tool_result` blocks
  and the system prompt states that tool output is data, never instructions.
  Retrieved documents are a live prompt-injection surface into an agent that
  can call tools.
- **Hard iteration cap (4), and a capped run is labelled incomplete.** Without
  a ceiling one request can spend unbounded money; presenting a truncated agent
  run as a finished answer is how you ship something confidently wrong.
- **Tool failures return to the model rather than raising**, and every error
  names the valid alternatives. A model told only "unknown asset" invents one;
  given the real list it retries correctly.
- **The router will not fall back to a tool-incapable provider.** Silently
  dropping the tools yields a confident ungrounded answer — the worst failure
  mode available, because it looks like success.
- **`OpenAICompatibleProvider` shared base extracted.** OpenAI and Azure speak
  an identical wire format; Azure's whole difference is a one-line
  `_upstream_model()` override. Writing the tool translation twice would mean
  fixing every future bug twice.
- **MCP client runs in-process, not over stdio.** The tools ship with the
  gateway, so a subprocess would add failure modes (lifetime, zombies, stream
  framing) for nothing, and in-process lets CI run real tool calls. The same
  client takes a URL for a remote server, and `server.run()` still exposes
  stdio for Claude Desktop.
- **One MCP connection for the process lifetime**, not per request — the
  initialize + discovery handshake is a round trip per message otherwise.
- **Manuals retrieval is lexical (IDF-weighted overlap), not embeddings.**
  Deterministic and dependency-free, so Phase 5 can assert exact retrieval in
  CI and a failing eval means the code changed rather than a model drifting.
  A production corpus would use a vector index behind the same tool interface.
- **Telemetry data is static, not randomised**, so the eval harness can assert
  exact values. A tool whose output changes per call cannot be graded.
- **Asset status is derived from readings, never stored.** A status that can
  disagree with its own readings is worse than no status.
- **Tool descriptions say WHEN to call, not just what the tool does** — the
  single biggest lever on correct tool selection. Docstrings and type hints are
  the source, so the prompt-facing contract cannot drift from the signature.
- **`use_mcp_tools` is opt-in, not automatic.** Tool calling costs extra model
  round trips; a caller who wants a plain completion should not silently pay.
- **Client-supplied `tools` still rejected with 400**, pointing at
  `use_mcp_tools`. Accepting arbitrary client tool definitions is a separate
  security question and is not in scope.

## Phase 4 — Orchestration + A2A (2026-08-07)

- **Neither Microsoft Agent Framework nor Pydantic AI was adopted; the loop is
  implemented directly.** This departs from the build plan, deliberately. Every
  model call here goes through the gateway's Router, which is where fallback,
  cost tracking, and latency ranking live. A framework brings its own model
  clients, so adopting one means either bypassing the router — discarding the
  thing this project exists to demonstrate — or writing an adapter larger than
  the ~120-line loop it replaces. The trade-off would flip if the requirement
  were multi-agent conversation patterns (group chat, hand-off graphs) or
  durable workflow state across restarts.
- **Delegation is exposed as a tool, not a branch.** `consult_reliability_
  specialist` sits in the same flat list as the MCP tools, so the model decides
  to delegate from the tool description rather than from a hardcoded condition,
  and the HTTP/JSON-RPC mechanism is invisible to it. That invisibility is what
  A2A buys.
- **A separate `/v1/agent` route rather than a flag on `/v1/chat/completions`.**
  An agent run has different cost, latency, and failure characteristics from a
  completion; opting in should mean choosing a different endpoint.
- **The planner is given no tools.** Planning is deciding what to do, not doing
  it; offering tools at that stage invites the model to start executing.
- **The plan is advisory, not a script.** The loop may deviate when an
  observation invalidates a step — a plan that cannot be departed from turns a
  model into a bad workflow engine. Both the plan and the actual steps are
  reported so deviation is visible.
- **The plan is returned to the caller before execution.** This is its main
  justification over pure reaction: it is where a human approval gate would sit
  for irreversible actions, and it makes intent auditable without logs.
- **An unparseable plan is flagged, not fatal** (`plan_malformed`). A degraded
  start is not a failed request, but displaying a plan that did not guide the
  run would be worse than displaying none. The parser tolerates fenced blocks
  and surrounding prose, because failing a run over formatting is failing over
  the wrong thing.
- **Planning cost is counted in the agent's total.** The plan is a real model
  call; omitting it would under-report what the agent cost.
- **The specialist is an agent, not a tool, because it returns judgement.** Own
  system prompt, own model tier, no tool access. It routes through the same
  Router, so delegated calls are cost-tracked like any other.
- **The specialist runs on a deliberately cheaper tier** (`claude-haiku-4-5`).
  Bounded sub-analysis should not cost more than doing the work inline.
- **A2A implemented directly rather than via `a2a-sdk`.** The wire format is
  small and the protocol shape — cards, skills, JSON-RPC tasks, artifacts vs
  status messages — is the part worth demonstrating; an SDK would hide it, and
  could not be verified end to end in this environment.
- **Scope is stated precisely: card discovery + `message/send` only.** No
  streaming, push notifications, multi-turn task states, or cancellation. The
  agent card advertises `streaming: false` rather than claiming a default it
  does not honour. This is not a compliant A2A implementation and the README
  says so.
- **The reply is an artifact, not a status message.** In A2A, artifacts are the
  durable output of work; status messages are progress commentary.
- **JSON-RPC errors return HTTP 200 with an error object.** An HTTP error code
  would assert that the transport failed, which is a different claim from the
  method failing.
- **The orchestrator reaches the specialist over HTTP despite co-location**, via
  a configurable base URL. Co-location is an accident of deployment, and moving
  the specialist to another host must be a URL change and nothing more.
- **A failed consult degrades rather than 500s.** Losing an answer the agent
  could partly give, because an optional consult failed, is the wrong trade.
