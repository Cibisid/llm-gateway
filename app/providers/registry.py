"""Builds the set of providers this process can actually use.

A provider is only registered if its credentials are present and complete.
That is what makes the service degrade honestly: with only ANTHROPIC_API_KEY
set, a request for a GPT model returns a clear "no provider configured" error
instead of failing deep inside an SDK with an authentication error.

Registration order is also the "order" routing strategy's ranking, so the order
of the blocks below is a routing decision, not just style.
"""

from __future__ import annotations

import logging

from app.config import ANTHROPIC_MODELS, OPENAI_MODELS, Settings
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.azure_openai_provider import AzureOpenAIProvider
from app.providers.base import Provider
from app.providers.openai_provider import OpenAIProvider

logger = logging.getLogger(__name__)


def build_providers(settings: Settings) -> list[Provider]:
    providers: list[Provider] = []

    if settings.anthropic_api_key:
        providers.append(
            AnthropicProvider(settings.anthropic_api_key, ANTHROPIC_MODELS)
        )
    else:
        logger.warning("ANTHROPIC_API_KEY not set — Anthropic models unavailable")

    if settings.openai_api_key:
        providers.append(OpenAIProvider(settings.openai_api_key, OPENAI_MODELS))
    else:
        logger.warning("OPENAI_API_KEY not set — OpenAI models unavailable")

    # Azure needs four settings, not one. Registering with a partial config
    # would produce a provider that fails on every call, so we require all four
    # and say precisely which are missing.
    azure_fields = {
        "AZURE_OPENAI_API_KEY": settings.azure_openai_api_key,
        "AZURE_OPENAI_ENDPOINT": settings.azure_openai_endpoint,
        "AZURE_OPENAI_API_VERSION": settings.azure_openai_api_version,
        "AZURE_OPENAI_DEPLOYMENT": settings.azure_openai_deployment,
    }
    missing = [name for name, value in azure_fields.items() if not value]
    if not missing:
        providers.append(
            AzureOpenAIProvider(
                api_key=settings.azure_openai_api_key,  # type: ignore[arg-type]
                endpoint=settings.azure_openai_endpoint,  # type: ignore[arg-type]
                api_version=settings.azure_openai_api_version,  # type: ignore[arg-type]
                deployment=settings.azure_openai_deployment,  # type: ignore[arg-type]
            )
        )
    elif len(missing) < len(azure_fields):
        # Some but not all set: almost certainly a misconfiguration rather than
        # a deliberate opt-out, so this is louder than the all-absent case.
        logger.warning(
            "Azure OpenAI partially configured — missing %s; provider not registered",
            ", ".join(missing),
        )
    else:
        logger.info("Azure OpenAI not configured — skipping")

    if not providers:
        logger.error("No provider credentials found; every request will fail")

    return providers
