"""Builds the set of providers this process can actually use.

A provider is only registered if its credential is present. That is what makes
the service degrade honestly: with only ANTHROPIC_API_KEY set, a request for a
GPT model returns a clear "no provider configured" error instead of failing
deep inside an SDK with an authentication error.
"""

from __future__ import annotations

import logging

from app.config import ANTHROPIC_MODELS, OPENAI_MODELS, Settings
from app.providers.anthropic_provider import AnthropicProvider
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

    if not providers:
        logger.error("No provider credentials found; every request will fail")

    return providers
