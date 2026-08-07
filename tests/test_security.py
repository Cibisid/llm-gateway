"""Phase 6 tests: auth, rate limiting, redaction, and the audit trail.

Security tests earn their keep by asserting the NEGATIVE — that an
unauthenticated request never reaches a provider, that a key never appears in a
log, that content is not recorded unless explicitly enabled. A security layer
that is only tested on its happy path is untested.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.logging_setup import (
    AUDIT_LOGGER_NAME,
    MAX_LOGGED_CHARS,
    attach_content,
    new_record,
)
from app.providers.base import ProviderResult, Usage
from app.security import (
    RateLimiter,
    key_fingerprint,
    parse_api_keys,
    redact,
    verify_api_key,
)
from tests.conftest import AUTH_HEADERS, TEST_API_KEY, FakeProvider


def _provider() -> FakeProvider:
    return FakeProvider(
        "fake",
        ("test-model",),
        result=ProviderResult(
            text="the answer",
            upstream_model="test-model",
            usage=Usage(8, 3),
            finish_reason="stop",
        ),
    )


def _body() -> dict:
    return {"model": "test-model", "messages": [{"role": "user", "content": "hi"}]}


class _AuditCapture(logging.Handler):
    """Collects audit records directly off the audit logger.

    pytest's caplog cannot see them, and that is by design rather than an
    inconvenience: the audit logger sets `propagate = False` so the audit
    stream can never be reformatted, filtered, or duplicated by the
    application logger's configuration. Attaching here tests the real logger.

    Must be attached AFTER the app starts, because startup calls
    configure_logging(), which clears existing handlers.
    """

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _capture_audit() -> _AuditCapture:
    handler = _AuditCapture()
    logging.getLogger(AUDIT_LOGGER_NAME).addHandler(handler)
    return handler


# --- key handling ----------------------------------------------------------


def test_fingerprint_is_stable_and_not_reversible():
    key = "sk-secret-value"
    fingerprint = key_fingerprint(key)

    assert fingerprint == key_fingerprint(key)
    assert key not in fingerprint
    assert len(fingerprint) == 12


def test_different_keys_get_different_fingerprints():
    assert key_fingerprint("key-a") != key_fingerprint("key-b")


def test_parse_api_keys_handles_whitespace_and_blanks():
    assert parse_api_keys(" a , b ,, c ") == frozenset({"a", "b", "c"})


def test_verify_returns_the_matched_key_and_rejects_others():
    allowed = frozenset({"alpha", "beta"})

    assert verify_api_key("beta", allowed) == "beta"
    assert verify_api_key("gamma", allowed) is None
    assert verify_api_key("", allowed) is None


def test_verify_rejects_a_prefix_of_a_valid_key():
    """Guards against a comparison that stops at the first mismatch."""
    assert verify_api_key("alph", frozenset({"alpha"})) is None


# --- authentication at the endpoint ---------------------------------------


def test_request_without_a_key_is_rejected_and_never_reaches_a_provider(
    configured_app,
):
    """The financial control: an unauthenticated request must not cost money."""
    provider = _provider()
    with configured_app([provider]) as client:
        response = client.post("/v1/chat/completions", json=_body())

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert provider.calls == [], "provider was called for an unauthenticated request"


def test_wrong_key_is_rejected_and_never_reaches_a_provider(configured_app):
    provider = _provider()
    with configured_app([provider]) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer wrong-key"},
            json=_body(),
        )

    assert response.status_code == 401
    assert provider.calls == []


def test_malformed_authorization_header_is_rejected(configured_app):
    with configured_app([_provider()]) as client:
        for header in ("", "Basic abc", f"Token {TEST_API_KEY}", "Bearer"):
            response = client.post(
                "/v1/chat/completions",
                headers={"Authorization": header},
                json=_body(),
            )
            assert response.status_code == 401, f"accepted header {header!r}"


def test_valid_key_is_accepted(configured_app):
    with configured_app([_provider()]) as client:
        response = client.post(
            "/v1/chat/completions", headers=AUTH_HEADERS, json=_body()
        )

    assert response.status_code == 200


def test_the_agent_endpoint_is_protected_too(configured_app):
    with configured_app([_provider()]) as client:
        assert client.post("/v1/agent", json=_body()).status_code == 401


def test_the_a2a_endpoint_is_protected_because_it_invokes_a_model(configured_app):
    """A2A is not a back door: it spends money, so it needs a credential."""
    with configured_app([_provider()]) as client:
        response = client.post(
            "/a2a/specialist",
            json={"jsonrpc": "2.0", "id": "1", "method": "message/send"},
        )

    assert response.status_code == 401


def test_service_fails_closed_when_no_keys_are_configured(monkeypatch):
    """An auth layer that disables itself when misconfigured is worse than none,
    because the deployment looks protected."""
    from fastapi.testclient import TestClient

    from app.config import get_settings

    monkeypatch.setenv("GATEWAY_API_KEYS", "")
    get_settings.cache_clear()
    provider = _provider()
    monkeypatch.setattr("app.main.build_providers", lambda settings: [provider])

    from app.main import app

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions", headers=AUTH_HEADERS, json=_body()
        )

    assert response.status_code == 503
    assert provider.calls == []
    get_settings.cache_clear()


def test_healthz_stays_open_and_leaks_no_configuration(configured_app):
    """A liveness probe that needs a credential fails during a credential
    outage — but it must expose capability only, never secrets."""
    with configured_app([_provider()]) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    body = json.dumps(response.json())
    assert TEST_API_KEY not in body
    assert "api_key" not in body.lower()


# --- rate limiting ---------------------------------------------------------


def test_limiter_allows_up_to_the_limit_then_refuses():
    limiter = RateLimiter(limit=3, window_seconds=60)

    assert [limiter.check("k")[0] for _ in range(3)] == [True, True, True]
    allowed, retry_after = limiter.check("k")
    assert allowed is False
    assert retry_after > 0


def test_limiter_tracks_each_key_separately():
    """One noisy client must not exhaust everyone else's quota."""
    limiter = RateLimiter(limit=1, window_seconds=60)

    assert limiter.check("key-a")[0] is True
    assert limiter.check("key-a")[0] is False
    assert limiter.check("key-b")[0] is True


def test_limiter_window_resets(monkeypatch):
    # A controlled clock from the start, so the bucket's own timestamp comes
    # from the same fake source the check does.
    clock = [1000.0]
    monkeypatch.setattr("app.security.time.monotonic", lambda: clock[0])

    limiter = RateLimiter(limit=1, window_seconds=60)
    assert limiter.check("k")[0] is True
    assert limiter.check("k")[0] is False

    clock[0] += 61
    assert limiter.check("k")[0] is True


def test_remaining_reports_the_quota_left():
    limiter = RateLimiter(limit=5, window_seconds=60)
    limiter.check("k")

    assert limiter.remaining("k") == 4
    assert limiter.remaining("never-seen") == 5


def test_over_limit_request_gets_429_with_retry_after(configured_app):
    provider = _provider()
    with configured_app([provider], RATE_LIMIT_PER_MINUTE=2) as client:
        codes = [
            client.post(
                "/v1/chat/completions", headers=AUTH_HEADERS, json=_body()
            ).status_code
            for _ in range(3)
        ]
        throttled = client.post(
            "/v1/chat/completions", headers=AUTH_HEADERS, json=_body()
        )

    assert codes == [200, 200, 429]
    assert throttled.status_code == 429
    assert "Retry-After" in throttled.headers
    # Throttled requests must not have reached the provider.
    assert len(provider.calls) == 2


# --- PII redaction ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "placeholder"),
    [
        ("contact me at alice@example.com", "[EMAIL]"),
        ("card 4111 1111 1111 1111 expires soon", "[CARD]"),
        ("server at 192.168.1.100 is down", "[IP]"),
        ("my key is sk-abcdefghijklmnopqrstuvwxyz123", "[API_KEY]"),
    ],
)
def test_redaction_masks_common_pii(raw, placeholder):
    cleaned = redact(raw)

    assert placeholder in cleaned
    # The sensitive fragment itself is gone.
    assert "alice@example.com" not in cleaned
    assert "4111 1111 1111 1111" not in cleaned


def test_redaction_leaves_ordinary_text_alone():
    text = "Pump 3 is at 87.4 degC, above the 80 degC limit."
    assert redact(text) == text


def test_redaction_is_documented_as_best_effort_not_a_guarantee():
    """It is regex: it will not catch a name or an unusual account format. The
    real control is not logging content by default."""
    assert redact("Patient Jane Smith, record 12-AB-99") == (
        "Patient Jane Smith, record 12-AB-99"
    )


# --- audit logging ---------------------------------------------------------


def test_content_is_not_logged_unless_explicitly_enabled():
    """The default that actually protects prompt data."""
    record = new_record(
        request_id="r", key_id="k", endpoint="/v1/chat/completions", requested_model="m"
    )
    attach_content(record, prompt="my secret prompt", response="answer", enabled=False)

    assert record.prompt_excerpt is None
    assert record.response_excerpt is None


def test_enabled_content_is_redacted_before_it_is_stored():
    record = new_record(
        request_id="r", key_id="k", endpoint="/v1/chat/completions", requested_model="m"
    )
    attach_content(
        record, prompt="email me at bob@example.com", response="ok", enabled=True
    )

    assert "[EMAIL]" in record.prompt_excerpt
    assert "bob@example.com" not in record.prompt_excerpt


def test_long_content_is_truncated():
    record = new_record(
        request_id="r", key_id="k", endpoint="/v1/chat/completions", requested_model="m"
    )
    attach_content(record, prompt="x" * 5000, response=None, enabled=True)

    assert "truncated" in record.prompt_excerpt
    assert len(record.prompt_excerpt) < 5000


def test_redaction_happens_before_truncation():
    """Truncating first could split a pattern and leave a partial credential."""
    prompt = "x" * (MAX_LOGGED_CHARS - 5) + " alice@example.com"
    record = new_record(
        request_id="r", key_id="k", endpoint="/e", requested_model="m"
    )
    attach_content(record, prompt=prompt, response=None, enabled=True)

    assert "alice@example.com" not in record.prompt_excerpt


def test_a_successful_request_writes_one_audit_record(configured_app):
    with configured_app([_provider()]) as client:
        capture = _capture_audit()
        client.post("/v1/chat/completions", headers=AUTH_HEADERS, json=_body())

    assert len(capture.records) == 1

    audit = capture.records[0].audit
    assert audit["endpoint"] == "/v1/chat/completions"
    assert audit["input_tokens"] == 8
    assert audit["output_tokens"] == 3
    assert audit["status_code"] == 200
    # Content is absent by default.
    assert audit["prompt_excerpt"] is None


def test_the_audit_record_identifies_the_key_by_fingerprint_never_by_value(
    configured_app,
):
    with configured_app([_provider()]) as client:
        capture = _capture_audit()
        client.post("/v1/chat/completions", headers=AUTH_HEADERS, json=_body())

    audit = capture.records[0].audit
    assert audit["key_id"] == key_fingerprint(TEST_API_KEY)
    assert TEST_API_KEY not in json.dumps(audit)


def test_failures_are_audited_too(configured_app):
    """An audit trail that records only successes cannot answer 'was this ever
    refused?' — which is what a compliance review asks."""
    with configured_app([_provider()]) as client:
        capture = _capture_audit()
        client.post(
            "/v1/chat/completions",
            headers=AUTH_HEADERS,
            json={"model": "nope", "messages": [{"role": "user", "content": "x"}]},
        )

    audit = capture.records[0].audit
    assert audit["status_code"] == 404
    assert audit["error"]


def test_audit_records_serialise_to_one_json_object_per_line(configured_app):
    """The audit log's consumers are queries, not people reading a terminal."""
    from app.logging_setup import JsonFormatter

    with configured_app([_provider()]) as client:
        capture = _capture_audit()
        client.post("/v1/chat/completions", headers=AUTH_HEADERS, json=_body())

    line = JsonFormatter().format(capture.records[0])

    parsed = json.loads(line)
    assert "\n" not in line
    assert parsed["request_id"]
