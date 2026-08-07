"""Settings and the model catalogue.

Settings come from the environment (via .env locally, Key Vault in Azure from
Phase 7). Nothing here reads a hardcoded secret.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Tolerate unrelated variables in the shared .env rather than crashing
        # the service on an env var that belongs to something else.
        extra="ignore",
    )

    anthropic_api_key: str | None = None
    openai_api_key: str | None = None

    azure_openai_api_key: str | None = None
    azure_openai_endpoint: str | None = None
    azure_openai_api_version: str | None = None
    azure_openai_deployment: str | None = None

    # Comma-separated client keys. Unused until Phase 6 — declared now so the
    # variable name is stable in .env.example and deployment configs.
    gateway_api_keys: str = ""

    log_level: str = "INFO"

    # Served when a request omits `model`. Cheapest capable option we can
    # actually reach with the keys available.
    default_model: str = "claude-haiku-4-5"


@lru_cache
def get_settings() -> Settings:
    """Cached so the .env file is parsed once per process, not per request."""
    return Settings()


# --- Model catalogue -------------------------------------------------------
#
# Which provider serves which model. The router reads this indirectly, via
# each adapter's `supported_models`; it is declared here so that adding a model
# is a one-line change in one file.
#
# Anthropic model ids and context windows verified against Anthropic's current
# model documentation on 2026-08-06.

ANTHROPIC_MODELS: tuple[str, ...] = (
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-sonnet-5",
    "claude-haiku-4-5",
)

# NOTE: implemented and unit-tested against a mocked client, but never
# exercised against the live OpenAI API — no key is available in this
# environment. Treat as unverified until that changes.
OPENAI_MODELS: tuple[str, ...] = (
    "gpt-4o",
    "gpt-4o-mini",
)
