# Documentation index

The explainers are named for the order they were built in. That order means
nothing to a reader arriving cold, so here is what each one actually covers.

## Start here

| Read this | If you want to know |
|---|---|
| [The Provider contract](phase1-EXPLAINER.md) | How the gateway talks to several vendors without any vendor leaking past the adapter — and why usage is normalised at that boundary |
| [Routing and fallback](phase2-EXPLAINER.md) | How a model request becomes a ranked list of candidates, why aliases are what make cost routing meaningful, and when a failure is worth retrying elsewhere |
| [Tool calling over MCP](phase3-EXPLAINER.md) | Why `mcp` 2.0 rather than the 1.x line the build plan assumed, and how the gateway acts as an MCP host |
| [Agent orchestration and A2A](phase4-EXPLAINER.md) | The plan → act → observe loop, why no agent framework was adopted, and the exact conditions under which that reverses |
| [Automated evaluation](phase5-EXPLAINER.md) | The golden set, the scoring, and the five real bugs it caught that unit tests missed. **The most interesting file in the project** |
| [Security](phase6-EXPLAINER.md) | Bearer auth that fails closed, per-key rate limiting, PII redaction, and the audit log |
| [Container, Azure IaC, and CI](phase7-EXPLAINER.md) | What is built and tested, and what is a design artefact that has never been applied |

## Also here

- **[design-decisions.md](design-decisions.md)** — the full running log of
  decisions and the reasoning behind each, including the ones that were
  reversed.
- **[responsible-ai.md](responsible-ai.md)** — control-to-clause mapping against
  the NIST AI RMF and the EU AI Act, with an explicit list of gaps rather than
  a claim of full coverage.

## A note on the honesty

Several of these documents lead with what is *not* verified. That is deliberate
and consistent across the project: the status table in the top-level
[README](../README.md) marks the OpenAI and Azure adapters as unproven and the
Bicep template as never applied, because a green checkmark that is not backed by
a run is worth less than an accurate gap list.
