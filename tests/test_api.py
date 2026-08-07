"""Endpoint tests via FastAPI's TestClient — no network, no keys."""

from __future__ import annotations

import pytest

from app.providers.base import ProviderError, ProviderResult, Usage
from tests.conftest import AUTH_HEADERS, FakeProvider


@pytest.fixture
def client_with(configured_app):
    """Build a TestClient whose router uses the given fake providers.

    Auth is genuinely configured rather than stubbed out, so every request
    below must present a real bearer token — the security layer is exercised
    by the whole file, not just the tests that name it.
    """
    return configured_app


def test_successful_completion_returns_openai_shape(client_with):
    provider = FakeProvider(
        "fake",
        ("test-model",),
        result=ProviderResult(
            text="the answer",
            upstream_model="test-model-v2",
            usage=Usage(input_tokens=8, output_tokens=3),
            finish_reason="stop",
        ),
    )
    with client_with([provider]) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=AUTH_HEADERS,
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200
    body = response.json()

    # The fields an existing OpenAI client library will read.
    assert body["object"] == "chat.completion"
    assert body["id"].startswith("chatcmpl-")
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["choices"][0]["message"]["content"] == "the answer"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"] == {
        "prompt_tokens": 8,
        "completion_tokens": 3,
        "total_tokens": 11,
    }

    # The gateway's own extension block — routing must be observable.
    assert body["gateway"]["provider"] == "fake"
    assert body["gateway"]["upstream_model"] == "test-model-v2"
    assert body["gateway"]["latency_ms"] >= 0


def test_streaming_is_rejected_rather_than_silently_ignored(client_with):
    """Accepting `stream: true` and returning a whole body would be a lie."""
    with client_with([FakeProvider("fake", ("test-model",))]) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=AUTH_HEADERS,
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )

    assert response.status_code == 400
    assert "not implemented" in response.json()["detail"].lower()


def test_tool_calling_is_rejected_until_phase_3(client_with):
    with client_with([FakeProvider("fake", ("test-model",))]) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=AUTH_HEADERS,
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function"}],
            },
        )

    assert response.status_code == 400


def test_unknown_model_is_404_not_502(client_with):
    """Routing failure happens before any network call, so it is not a bad gateway."""
    with client_with([FakeProvider("fake", ("test-model",))]) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=AUTH_HEADERS,
            json={"model": "nope", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 404


def test_upstream_failure_is_502_not_500(client_with):
    """The gateway is fine; the thing it depends on is not."""
    provider = FakeProvider(
        "fake",
        ("test-model",),
        error=ProviderError("fake", "upstream down", retryable=True),
    )
    with client_with([provider]) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=AUTH_HEADERS,
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 502


def test_empty_messages_is_rejected_by_validation(client_with):
    with client_with([FakeProvider("fake", ("test-model",))]) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=AUTH_HEADERS,
            json={"model": "test-model", "messages": []},
        )

    assert response.status_code == 422


def test_unknown_fields_are_rejected_not_silently_dropped(client_with):
    with client_with([FakeProvider("fake", ("test-model",))]) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=AUTH_HEADERS,
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "hi"}],
                "frequency_penalty": 0.5,
            },
        )

    assert response.status_code == 422


def test_healthz_reports_real_capability(client_with):
    providers = [
        FakeProvider("a", ("model-a",)),
        FakeProvider("b", ("model-b",)),
    ]
    with client_with(providers) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["available_models"] == ["model-a", "model-b"]


def test_healthz_is_honest_when_nothing_is_configured(client_with):
    with client_with([]) as client:
        response = client.get("/healthz")

    assert response.json()["available_models"] == []
