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

    # Comma-separated client keys. Empty means the service refuses every
    # request with 503 — it fails CLOSED rather than running unauthenticated.
    gateway_api_keys: str = ""

    rate_limit_per_minute: int = 60

    # OFF by default. Prompts routinely contain customer data and pasted
    # credentials; logging them by default would push all of that into whatever
    # log sink the deployment uses. When enabled, content is redacted and
    # truncated — but the default is the control that actually protects it.
    audit_log_content: bool = False

    #: Where the JSON audit stream goes. Empty means stdout, so a container
    #: platform collects it without the service needing a writable volume.
    audit_log_path: str = ""

    log_level: str = "INFO"

    # Served when a request omits `model`. Cheapest capable option we can
    # actually reach with the keys available.
    default_model: str = "claude-haiku-4-5"

    # How the router ranks candidates: "order" | "cost" | "latency".
    # Defaults to "order" because it is predictable and requires no
    # measurements; "cost" is the interesting one to demo.
    routing_strategy: str = "order"

    # Base URL the orchestrator uses to reach the A2A specialist agent. It
    # points at this service today because the two are co-located, but it is a
    # setting rather than a constant precisely so the specialist can move to
    # another host without a code change.
    self_base_url: str = "http://gateway.local"


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

# Azure serves OpenAI models through a customer-specific *deployment name*
# rather than the public model id, so the servable id is read from settings at
# registration time (see app/providers/registry.py) rather than listed here.


# --- Model aliases ---------------------------------------------------------
#
# An alias lets a client express intent ("give me something that can answer
# this") instead of naming a model. The router expands the alias to these
# concrete candidates and ranks them by the active strategy — which is what
# makes cost-based routing meaningful rather than decorative. Members are
# listed cheapest-first so the "order" strategy is also a sensible default.
#
# Members that no configured provider can serve are skipped silently, so an
# alias still works when only some keys are present.

MODEL_ALIASES: dict[str, tuple[str, ...]] = {
    "auto": (
        "claude-haiku-4-5",
        "gpt-4o-mini",
        "claude-sonnet-5",
        "gpt-4o",
        "claude-opus-5",
    ),
    # Cheap, fast tier — for classification and extraction rather than reasoning.
    "auto-cheap": ("claude-haiku-4-5", "gpt-4o-mini"),
    # Highest-capability tier, cost secondary.
    "auto-quality": ("claude-opus-5", "gpt-4o", "claude-sonnet-5"),
}
