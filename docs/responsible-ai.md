# Responsible AI — controls, and where they fall short

This document maps controls that **exist in this repository** to the NIST AI
Risk Management Framework and the EU AI Act. Every row names the file that
implements it, so a claim can be checked against code rather than taken on
trust.

It also records what is **not** covered. A responsible-AI document that lists
only strengths is a marketing document; the gaps below are the honest part, and
they are the part worth discussing.

---

## What this system is, for classification purposes

An API gateway that routes prompts to third-party LLMs, runs retrieval and
telemetry-lookup tools, and returns generated text about industrial equipment.

**Under the EU AI Act this is not, as built, a high-risk system.** It is a
general-purpose assistant over maintenance documentation, and the obligations
below are mostly those of a *deployer* of a general-purpose AI model, plus the
transparency duties in Article 50.

**It would become high-risk** if its output were wired into anything that
controls equipment, decides whether a machine is safe to operate, or feeds a
safety-instrumented system — Annex I / Annex III territory. That change would be
a deployment decision, not a code change, which is exactly why it is written
down here: the classification depends on how the output is used, and nothing in
the code prevents someone using it that way.

---

## NIST AI RMF mapping

### GOVERN — policies, accountability, culture

| Sub-function | Control in this repo | Where |
|---|---|---|
| GOVERN 1.1 — legal/regulatory requirements understood | This document; classification stated above with the trigger that would change it | `docs/responsible-ai.md` |
| GOVERN 1.2 — trustworthiness characteristics integrated | Evaluation gates every push in CI; honesty rules are enforced in code review and stated in `CLAUDE.md` | `.github/workflows/ci.yml`, `CLAUDE.md` |
| GOVERN 1.4 — risk management documented | Every design decision recorded with its reasoning, including rejected options | `docs/design-decisions.md` |
| GOVERN 4.1 — critical thinking about risk encouraged | Each phase's EXPLAINER carries an explicit "honest status" section listing what is unverified | `docs/phase*-EXPLAINER.md` |
| GOVERN 6.1 — third-party risks addressed | Provider adapters isolate vendor coupling; fallback across providers; per-provider failure classification | `app/providers/`, `app/router.py` |

### MAP — context and risk identification

| Sub-function | Control | Where |
|---|---|---|
| MAP 1.1 — intended purpose defined | Purpose and the boundary that would make it high-risk, above | `docs/responsible-ai.md`, `README.md` |
| MAP 2.3 — system capability and limits documented | Every phase states what is implemented, what is not, and what is unverified — e.g. A2A implements card discovery and `message/send` only, and says so | `docs/phase*-EXPLAINER.md` |
| MAP 5.1 — impacts of failure characterised | Threat model with an explicit "does not defend against" list | `app/security.py`, `docs/phase6-EXPLAINER.md` |

### MEASURE — analysis and tracking

| Sub-function | Control | Where |
|---|---|---|
| MEASURE 2.3 — performance measured against requirements | Golden-set evaluation, run on every push | `eval/runner.py`, `eval/golden_set.yaml` |
| MEASURE 2.5 — validity and reliability | Tool-call correctness, grounding checks, similarity floor, LLM-judge; every case anchored by at least one decidable check, enforced by a meta-test | `eval/judges.py`, `tests/test_eval.py` |
| MEASURE 2.6 — safety risks evaluated | Dedicated anti-hallucination cases: an answer about a non-existent asset must contain no reading; an answer with no matching manual section must cite no section id | `eval/golden_set.yaml` |
| MEASURE 2.7 — security and resilience | Auth, rate limiting, and secret-scanning tests; CI fails on a secret-shaped string anywhere in git history | `tests/test_security.py`, `.github/workflows/ci.yml` |
| MEASURE 2.8 — transparency of operation | Every response reports which provider served it, what it cost, which tools ran, and the agent's plan | `app/schemas.py` (`GatewayMetadata`) |
| MEASURE 2.11 — harmful bias | **NOT MEASURED.** See gaps. |
| MEASURE 3.1 — tracked over time | Per-request cost, tokens, and latency in the audit log; CI runs the eval on every push | `app/logging_setup.py` |

### MANAGE — risk response

| Sub-function | Control | Where |
|---|---|---|
| MANAGE 1.2 — risks prioritised and treated | Retryable vs non-retryable failure handling; fail-closed auth; hard iteration caps on agent loops | `app/router.py`, `app/security.py`, `app/tool_loop.py` |
| MANAGE 2.2 — mechanisms to sustain value | Provider fallback; honest degradation when a specialist or tool is unreachable | `app/router.py`, `app/orchestrator.py` |
| MANAGE 2.4 — mechanisms to supersede or deactivate | Iteration caps stop runaway agents; rate limits bound spend; a capped run is **labelled incomplete** rather than presented as finished | `app/tool_loop.py`, `app/orchestrator.py` |
| MANAGE 4.1 — post-deployment monitoring | Structured audit log with cost, tokens, tools, and failures, shipped to Log Analytics | `app/logging_setup.py`, `infra/main.bicep` |

---

## EU AI Act mapping

### Article 50 — transparency obligations

| Obligation | How it is met | Where |
|---|---|---|
| Users informed they are interacting with an AI system | The API is explicitly an LLM gateway; every response carries `gateway.provider` and `gateway.upstream_model` naming the model that generated it | `app/schemas.py` |
| AI-generated output identifiable | Every response identifies its generating model; agent responses additionally expose the plan and every tool call | `app/schemas.py` |

### Deployer obligations for general-purpose AI

| Obligation | How it is met | Where |
|---|---|---|
| Technical documentation | Per-phase explainers plus a full decision log with reasoning | `docs/` |
| Record-keeping / logging (Art. 12 pattern) | One structured audit record per request — including refusals — with model, cost, tokens, tools, and outcome | `app/logging_setup.py` |
| Human oversight (Art. 14 pattern) | The agent's plan is returned **before execution**, which is where an approval gate belongs. Answers are grounded in cited manual sections so a human can verify a claim rather than trust it | `app/orchestrator.py`, `mcp_server/tools/manuals.py` |
| Accuracy, robustness, cybersecurity (Art. 15 pattern) | Golden-set evaluation in CI; auth and rate limiting; tool output treated as untrusted; input validation rejecting unknown fields | `eval/`, `app/security.py`, `app/mcp_client.py` |
| Data governance | Prompt content is not logged by default; PII redaction when it is; no secret in image, template, or deployment history | `app/logging_setup.py`, `infra/main.bicep` |

**Article 14 caveat.** The plan is *exposed*, which makes oversight possible. No
approval gate is *enforced* — nothing blocks execution pending a human decision.
The hook exists; the control does not. Stated plainly because "supports human
oversight" is easy to claim and easy to overstate.

---

## Prompt injection

Retrieved documents and tool results are **untrusted input**. If someone could
place text in the manuals corpus, `"ignore previous instructions and ..."` would
otherwise be a direct path into an agent that can call tools.

Mitigations, all real: results are returned inside `tool_result` blocks rather
than concatenated into the prompt as if the user wrote them; the system prompt
states that tool output is reference data and never instructions
(`app/tool_loop.py`, `app/orchestrator.py`); the eval judge is separately told
to ignore any instructions inside the answer it is grading, because the grader
is a target too.

**These are mitigations, not prevention.** No known technique eliminates prompt
injection. The realistic control is that the tools here are read-only — the
worst outcome is a misleading answer, not an action. That property is what makes
the residual risk acceptable, and **it would stop being true the moment a
write-capable tool is added.**

---

## Gaps — what this does not do

1. **No bias or fairness evaluation.** The golden set measures grounding and
   correctness, not disparate performance across groups. For industrial
   telemetry this is a lower-order risk, but it is unmeasured, not absent.
2. **No enforced human-in-the-loop.** The plan is visible; nothing gates on it.
3. **No content safety filter on output.** The system relies on the providers'
   own safety layers. There is no independent check on what is returned.
4. **No model card or provenance record for the upstream models.** The gateway
   records which model answered but does not carry documentation about those
   models' training data or known limitations.
5. **Rate limiting and latency tracking are per-process.** With multiple
   replicas, the effective limits multiply. A shared store is the fix.
6. **No key rotation, expiry, or revocation.** Gateway keys are static config.
7. **The Azure template has never been deployed.** It expresses intent and is
   unvalidated against ARM.
8. **The model-dependent evaluation cases have never been executed.** No API key
   was available; they are reported as skipped, never as passed. Every claim
   about *model behaviour* in this repository is therefore untested — as
   distinct from claims about the system's plumbing, which is tested.

Gap 8 is the one to read twice. The infrastructure for measuring AI output
quality is built and working; the measurements themselves have not been taken.

---

## Provenance of the claims above

Everything in this document points at a file. Nothing is aspirational. Where a
control is partial, the row says so; where it is missing, it is in the gaps
list rather than absent from the page.
