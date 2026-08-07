# Project: AI Gateway & Agent Platform

## What this is
A secure, cloud-native FastAPI service that routes across multiple LLM
providers, runs agentic tools over MCP, tracks cost per request, and
evaluates its own output quality in CI. Deployed to Azure Container Apps.

## Hard rules
- Build ONE phase at a time. Do not start the next phase until the current
  one runs and its tests pass. Commit after each phase.
- Every non-obvious block gets a one-line "why" comment.
- Maintain docs/design-decisions.md: append every real decision (library
  choice, routing rule, trade-off) with a one-line reason.
- Describe only what actually runs. Never label a stub as implemented.
- No secret ever enters git. Keys come from environment / Key Vault only.
  .env is gitignored; .env.example holds placeholder names only.
- Pin exact dependency versions. For fast-moving libraries (MCP SDK,
  Microsoft Agent Framework, A2A), fetch current docs before importing.

## Conventions
- Python 3.11+, FastAPI, Pydantic v2, async where it matters.
- Provider integrations implement the abstract contract in
  app/providers/base.py. Adding a provider must not require touching the
  router beyond registering it.
- Tests use pytest and live in tests/. Every phase ships tests.
- Type hints everywhere. Prefer small, single-responsibility modules.

## The four files the human owner must be able to explain
app/router.py, app/providers/base.py, app/orchestrator.py, eval/runner.py.
Keep these especially clear and well-commented.

## Environment facts (verified 2026-08-06)
- Local Python is 3.14.4; all pinned deps support >=3.10.
- Only ANTHROPIC_API_KEY is available. The Anthropic adapter is the live,
  end-to-end verified path. The OpenAI and Azure OpenAI adapters are
  written to the same contract and covered by mocked tests ONLY — do not
  describe them as verified anywhere in docs or README until a real key
  exercises them.
- Azure CLI is not installed yet; needed for Phase 7.
