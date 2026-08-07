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
