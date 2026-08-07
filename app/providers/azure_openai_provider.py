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
    configuration, and `_upstream_model()` is overridden to send the deployment
    name rather than whatever the client asked for.

    That override is the entire difference between this adapter and the OpenAI
    one — exactly the kind of vendor-specific weirdness the Provider contract
    exists to absorb, and the router never sees it.
"""

from __future__ import annotations

import openai

from app.providers._openai_compatible import OpenAICompatibleProvider
from app.schemas import ChatCompletionRequest


class AzureOpenAIProvider(OpenAICompatibleProvider):
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

    def _upstream_model(self, request: ChatCompletionRequest) -> str:
        # Azure routes on deployment name, not public model id.
        return self._deployment
