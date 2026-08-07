"""OpenAI adapter.

STATUS: implemented against the same contract as the Anthropic adapter and
covered by unit tests with a mocked client. It has NEVER been run against the
live OpenAI API, because no key is available in this environment. Do not
describe this path as verified.

Almost all of the behaviour lives in `_openai_compatible.py`, shared with the
Azure adapter — the two speak an identical wire format and differ only in
client construction and model addressing. This class is deliberately thin;
that thinness is the evidence the shared base is factored correctly.
"""

from __future__ import annotations

import openai

from app.providers._openai_compatible import OpenAICompatibleProvider


class OpenAIProvider(OpenAICompatibleProvider):
    name = "openai"

    def __init__(self, api_key: str, models: tuple[str, ...]) -> None:
        self._client = openai.AsyncOpenAI(api_key=api_key)
        self.supported_models = models
