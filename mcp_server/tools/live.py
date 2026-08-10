"""Shared plumbing for tools that call live external APIs.

Everything a tool needs to talk to the outside world safely, in one place, so
each domain module is about its domain rather than about HTTP.

THREE THINGS THIS SOLVES, ALL LEARNED THE HARD WAY

1. PUBLIC APIS ARE SLOW AND SOMETIMES DOWN.
   The Environment Agency endpoint timed out on the first call while building
   this and answered in two seconds on the second. A tool that hangs stalls
   the whole agent loop and the user watches a spinner. Every request here has
   a hard timeout, and a timeout produces a usable answer ("the service did not
   respond") rather than an exception.

2. A DEMO SHOULD NOT HAMMER A FREE PUBLIC SERVICE.
   Ten people asking about the same river in a demo is ten identical requests.
   A short-lived cache makes that one request. The TTL is per-domain and set to
   just under how often the source actually updates — caching river levels for
   an hour would serve stale data, caching them for zero seconds is rude.

3. FAILURE MUST REACH THE MODEL, NOT THE USER.
   Every failure returns a dict with an `error` key. The model reads that,
   tells the user the data source is unavailable, and does NOT invent a
   number to fill the gap. Raising would turn a temporary outage into a 500 on
   a question the agent could have partly answered.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: Hard ceiling on any single external call. Beyond this the agent loop is
#: stalling and the user is staring at nothing.
REQUEST_TIMEOUT_S = 20.0

#: Sent on every request. Public APIs are entitled to know who is calling, and
#: an anonymous scraper is the first thing an operator blocks.
USER_AGENT = "llm-gateway-demo/0.1 (+https://github.com/Cibisid/llm-gateway)"

_cache: dict[str, tuple[float, Any]] = {}


def cached_get(
    url: str,
    *,
    ttl_s: float,
    params: dict | None = None,
    timeout_s: float = REQUEST_TIMEOUT_S,
) -> dict:
    """GET and parse JSON, with a TTL cache and errors returned as data.

    Returns either the parsed payload or ``{"error": ..., "source": ...}``.
    Never raises — see point 3 in the module docstring.
    """
    key = f"{url}?{sorted((params or {}).items())}"
    now = time.monotonic()

    hit = _cache.get(key)
    if hit and now - hit[0] < ttl_s:
        return hit[1]

    try:
        response = httpx.get(
            url,
            params=params,
            timeout=timeout_s,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.TimeoutException:
        logger.warning("live source timed out: %s", url)
        return {
            "error": "The data source did not respond in time.",
            "source": url,
            "advice": "Say the live data is temporarily unavailable. Do not estimate a value.",
        }
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        logger.warning("live source returned %s: %s", status, url)
        # 404 means "you asked for something that does not exist", which is a
        # DIFFERENT problem from "the service is broken" and needs different
        # advice. Collapsing them sends the model down the wrong path: it would
        # tell the user to try later when the real fix is to correct the input.
        if status == 404:
            return {
                "not_found": True,
                "error": "The data source has no record matching that request.",
                "source": url,
                "advice": "The identifier is probably wrong. Do not retry unchanged; check the input.",
            }
        return {
            "error": f"The data source returned HTTP {status}.",
            "source": url,
            "advice": "Say the live data is temporarily unavailable. Do not estimate a value.",
        }
    except ValueError as exc:
        # The body was not JSON. In practice this means the upstream returned an
        # empty body or an HTML error page for an unknown identifier, so it is
        # far more often a bad input than a broken service.
        logger.warning("live source returned non-JSON: %s (%s)", url, exc)
        return {
            "not_found": True,
            "error": "The data source returned no usable data for that request.",
            "source": url,
            "advice": "The identifier is probably wrong or not covered. Do not invent a value.",
        }
    except Exception as exc:  # noqa: BLE001 - any failure must become data
        logger.warning("live source failed: %s (%s)", url, exc)
        return {
            "error": f"Could not reach the data source: {exc}",
            "source": url,
            "advice": "Say the live data is temporarily unavailable. Do not estimate a value.",
        }

    # Errors are deliberately NOT cached: a transient outage should not be
    # replayed to every user for the next five minutes.
    _cache[key] = (now, payload)
    return payload


def clear_cache() -> None:
    """Drop everything. Used by tests that must see a real call happen."""
    _cache.clear()


def cache_size() -> int:
    return len(_cache)
