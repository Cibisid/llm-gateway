"""The source contract.

Adding a data source must never require editing the pipeline, the API or the UI.
A source declares what kind of records it produces, and the ingest layer decides
what to do with them. Failure is returned as data (ok=False plus advice), never
raised -- one dead scraper must not take the dashboard down with it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# What a source contributes. A source may emit more than one kind.
KIND_CATALOG = "catalog"   # releases: brand, silhouette, SKU, retail, date
KIND_MARKET = "market"     # bids, asks, sales
KIND_NEWS = "news"         # editorial / social mentions


@dataclass
class SourceResult:
    name: str
    kind: str
    ok: bool
    records: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""
    latency_ms: int = 0
    is_demo: bool = False

    @property
    def count(self) -> int:
        return len(self.records)


@runtime_checkable
class Source(Protocol):
    name: str
    kind: str
    # Sources that scrape a site the operator may not be entitled to scrape are
    # opt-in; the registry refuses to run them unless explicitly enabled.
    requires_optin: bool

    def fetch(self, ctx: "FetchContext") -> SourceResult: ...


@dataclass
class FetchContext:
    """Everything a source needs, injected rather than imported.

    Sources never touch the DB directly -- they read `sneakers` and return records.
    That keeps them trivially testable with no database at all.
    """
    client: Any                      # httpx.Client, or a fake in tests
    sneakers: list[dict[str, Any]]   # current catalog rows
    today: Any
    user_agent: str
    timeout_s: float


def timed(fn):
    """Wrap a fetch so latency and unexpected exceptions become part of the result."""
    def wrapper(self, ctx: FetchContext) -> SourceResult:
        started = time.perf_counter()
        try:
            result = fn(self, ctx)
        except Exception as exc:  # noqa: BLE001 - a source must never crash the scheduler
            result = SourceResult(
                name=self.name, kind=self.kind, ok=False,
                detail=f"{type(exc).__name__}: {exc}",
            )
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result
    return wrapper
