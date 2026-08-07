"""Authentication, rate limiting, and PII redaction.

THREAT MODEL — what this actually defends against, and what it does not.

Defends against:
  - Anonymous use of the endpoint. Without auth, anyone who can reach the port
    spends your provider credits; that is the primary risk of an LLM gateway
    and it is a financial one, not just a data one.
  - One client exhausting the budget or the upstream rate limit for everyone.
  - Prompts and tool output leaking verbatim into logs and log aggregators.
  - Timing attacks on key comparison.

Does NOT defend against:
  - A stolen key. There is no key rotation, expiry, or revocation list here —
    keys are static config. Real deployments need a key service.
  - A distributed attacker: the rate limiter is per-process, so N replicas
    permit N times the limit. Stated plainly rather than papered over.
  - Prompt injection from tool output. That is mitigated in the tool layer
    (see app/mcp_client.py and the system prompts), not here, and mitigation is
    not prevention.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import time
from dataclasses import dataclass, field

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

#: Requests allowed per key per window, and the window length in seconds.
DEFAULT_RATE_LIMIT = 60
DEFAULT_WINDOW_SECONDS = 60


def key_fingerprint(api_key: str) -> str:
    """A short, stable, non-reversible identifier for a key.

    Logs need to distinguish clients, but a log line containing the key itself
    turns every log sink — and every screenshot of one — into a credential
    leak. SHA-256 truncated to 12 hex chars: enough to correlate requests,
    useless for authenticating.
    """
    return hashlib.sha256(api_key.encode()).hexdigest()[:12]


def parse_api_keys(raw: str) -> frozenset[str]:
    return frozenset(k.strip() for k in raw.split(",") if k.strip())


def verify_api_key(presented: str, allowed: frozenset[str]) -> str | None:
    """Constant-time key check. Returns the matched key, or None.

    `secrets.compare_digest` rather than `in` or `==`: a plain comparison
    short-circuits on the first differing byte, so response timing leaks the
    key prefix and an attacker can recover it byte by byte. Iterating over
    every candidate — with no early exit on match — keeps the work uniform.
    """
    matched: str | None = None
    for candidate in allowed:
        if secrets.compare_digest(presented, candidate):
            matched = candidate
    return matched


@dataclass
class _Bucket:
    count: int = 0
    window_start: float = field(default_factory=time.monotonic)


class RateLimiter:
    """Fixed-window per-key rate limiter.

    Fixed window, not a token bucket, deliberately: it is trivial to explain,
    trivial to verify, and its one weakness (up to 2x the limit across a window
    boundary) is acceptable when the goal is stopping runaway spend rather than
    smoothing traffic.

    HONEST LIMITATION: in-memory and per-process. Three replicas permit three
    times the configured limit. A real deployment puts this in Redis or at the
    ingress. Documented here rather than discovered in production.
    """

    def __init__(
        self,
        limit: int = DEFAULT_RATE_LIMIT,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._buckets: dict[str, _Bucket] = {}

    def check(self, key_id: str) -> tuple[bool, int]:
        """Return (allowed, seconds_until_reset). Counts the request if allowed."""
        now = time.monotonic()
        bucket = self._buckets.get(key_id)

        if bucket is None or now - bucket.window_start >= self._window:
            self._buckets[key_id] = _Bucket(count=1, window_start=now)
            return True, self._window

        retry_after = int(self._window - (now - bucket.window_start)) + 1
        if bucket.count >= self._limit:
            return False, retry_after

        bucket.count += 1
        return True, retry_after

    def remaining(self, key_id: str) -> int:
        bucket = self._buckets.get(key_id)
        if bucket is None or time.monotonic() - bucket.window_start >= self._window:
            return self._limit
        return max(0, self._limit - bucket.count)


# --- PII redaction ---------------------------------------------------------
#
# Applied to any prompt or tool output that reaches a log. Regex-based, which
# means it is a REDUCTION IN EXPOSURE, NOT A GUARANTEE: it will not catch a
# name, an address, or an account number in an unusual format. It is a
# defence-in-depth measure layered under "do not log content at all by
# default", which is the control that actually protects the data.

_REDACTIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("[EMAIL]", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    # ORDER MATTERS from here down. The specific patterns must run before the
    # broad ones: the loose PHONE pattern matches dot-separated digit runs, so
    # an IP address would be masked as [PHONE] if PHONE ran first — technically
    # still redacted, but a misleading label in an audit trail is its own bug.
    ("[IP]", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    # Payment-card-shaped digit runs, with optional separators.
    ("[CARD]", re.compile(r"\b(?:\d[ -]*?){13,19}\b")),
    ("[PHONE]", re.compile(r"\+?\d[\d\s().-]{8,}\d")),
    # Provider API keys, in case a user pastes one into a prompt — which they
    # do, and which would otherwise be written straight to the audit log.
    ("[API_KEY]", re.compile(r"\b(?:sk|pk|api)[-_][A-Za-z0-9\-_]{16,}\b")),
)


def redact(text: str) -> str:
    """Mask common PII patterns. Order matters: API keys and emails are matched
    before the broad digit-run patterns, which would otherwise consume them."""
    for placeholder, pattern in _REDACTIONS:
        text = pattern.sub(placeholder, text)
    return text


# --- FastAPI dependency ----------------------------------------------------


@dataclass(frozen=True)
class AuthContext:
    key_id: str
    rate_limit_remaining: int


async def require_api_key(request: Request) -> AuthContext:
    """Authenticate and rate-limit, BEFORE any provider call.

    Order is load-bearing: an unauthenticated request must never reach a
    provider, because reaching one costs money. Rate limiting comes second, for
    the same reason.
    """
    settings = request.app.state.settings
    allowed: frozenset[str] = request.app.state.api_keys

    if not allowed:
        # Fail CLOSED. An auth layer that disables itself when misconfigured is
        # worse than no auth layer, because it looks protected.
        raise HTTPException(
            status_code=503,
            detail=(
                "Gateway is not configured with any API keys. Set "
                "GATEWAY_API_KEYS to enable the service."
            ),
        )

    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        raise HTTPException(
            status_code=401,
            detail="Missing or malformed Authorization header. Expected: Bearer <key>.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    matched = verify_api_key(presented, allowed)
    if matched is None:
        # The failure is logged by fingerprint of the PRESENTED value, so
        # repeated probing is visible without ever recording a credential.
        logger.warning(
            "auth failed presented_fingerprint=%s path=%s",
            key_fingerprint(presented),
            request.url.path,
        )
        raise HTTPException(
            status_code=401,
            detail="Invalid API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    key_id = key_fingerprint(matched)
    limiter: RateLimiter = request.app.state.rate_limiter
    allowed_now, retry_after = limiter.check(key_id)
    if not allowed_now:
        logger.warning("rate limit exceeded key_id=%s", key_id)
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded ({settings.rate_limit_per_minute}/min).",
            headers={"Retry-After": str(retry_after)},
        )

    return AuthContext(key_id=key_id, rate_limit_remaining=limiter.remaining(key_id))
