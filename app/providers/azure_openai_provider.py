"""Azure OpenAI adapter.

STATUS: implemented and unit-tested with a mocked client. NEVER run against a
live Azure OpenAI resource — no endpoint or key is available in this
environment. Do not describe this path as verified.

WHY IT EXISTS
    Azure OpenAI is the same models behind a different front door: the same
    `openai` SDK, a different client class, a different auth model, and — the
    part that actually matters for routing — a different way of naming models.

THE ONE REAL DIFFERENCE: DEPLOYMENT NAMES
    OpenAI addresses a model by its public id ("gpt-4o"). Azure addresses a
    *deployment* — a customer-chosen name for a model you provisioned in your
    own resource. The deployment might be called "gpt-4o", or "prod-chat", or
    anything else. So the model id this adapter advertises comes from
    configuration, not from a constant, and the `model=` it sends upstream is
    the deployment name rather than the public model id.

    This is exactly the kind of vendor-specific weirdness the Provider contract
    exists to absorb: the router ranks and falls back over this adapter without
    knowing that "the model id" means something different here.
"""

from __future__ import annotations

import openai

from app.providers.base import Provider, ProviderError, ProviderResult, Usage
from app.schemas import ChatCompletionRequest

_FINISH_REASON_MAP = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_calls",
    "content_filter": "content_filter",
    "function_call": "tool_calls",
}


class AzureOpenAIProvider(Provider):
    name = "azure_openai"

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str,
        api_version: str,
        deployment: str,
    ) -> None:
        self._client = openai.AsyncAzureOpenAI(
            api_key=api_key,
            azure_endpoint=endpoint,
            api_version=api_version,
        )
        self._deployment = deployment
        # The gateway advertises the deployment name as the servable model id,
        # because that is what a caller must ask for to reach this resource.
        self.supported_models = (deployment,)

    async def chat(self, request: ChatCompletionRequest) -> ProviderResult:
        kwargs: dict = {
            # Azure routes on deployment name, not public model id.
            "model": self._deployment,
            "messages": [
                {"role": m.role, "content": m.content} for m in request.messages
            ],
        }
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature

        try:
            response = await self._client.chat.completions.create(**kwargs)
        except openai.APIStatusError as exc:
            retryable = exc.status_code == 429 or exc.status_code >= 500
            raise ProviderError(self.name, str(exc), retryable=retryable) from exc
        except openai.APIConnectionError as exc:
            raise ProviderError(self.name, str(exc), retryable=True) from exc

        choice = response.choices[0]
        usage = response.usage
        return ProviderResult(
            text=choice.message.content or "",
            upstream_model=response.model,
            usage=Usage(
                input_tokens=usage.prompt_tokens if usage else 0,
                output_tokens=usage.completion_tokens if usage else 0,
            ),
            finish_reason=_FINISH_REASON_MAP.get(choice.finish_reason or "", "stop"),
        )
