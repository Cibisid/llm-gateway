"""Ingest: run sources, write records, record health, recompute heat.

This is the only module that both talks to sources and touches the DB. Sources
stay pure (fetch -> records) and the pipeline stays pure (numbers -> numbers),
which is what makes both of them testable without a database or a network.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

from ..config import SETTINGS
from ..db import connect, one, query, tx
from ..pipeline import heat as heat_engine
from . import seed as seed_source
from .base import KIND_CATALOG, KIND_MARKET, KIND_NEWS, FetchContext, SourceResult


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# bootstrap (offline / demo)
# --------------------------------------------------------------------------

def upsert_sneaker(conn, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO sneaker (id, brand, model, colorway, sku, silhouette, collab,
                             retail_usd, release_date, release_type, region, gender,
                             image_url, image_slug, market_ref_json, colors_json, tags_json, provenance, as_of)
        VALUES (:id,:brand,:model,:colorway,:sku,:silhouette,:collab,:retail_usd,
                :release_date,:release_type,:region,:gender,:image_url,:image_slug,:market_ref_json,
                :colors_json,:tags_json,:provenance,:as_of)
        ON CONFLICT(id) DO UPDATE SET
            brand=excluded.brand, model=excluded.model, colorway=excluded.colorway,
            sku=COALESCE(excluded.sku, sneaker.sku),
            retail_usd=COALESCE(excluded.retail_usd, sneaker.retail_usd),
            release_date=COALESCE(excluded.release_date, sneaker.release_date),
            release_type=excluded.release_type,
            image_url=COALESCE(excluded.image_url, sneaker.image_url),
            image_slug=COALESCE(excluded.image_slug, sneaker.image_slug),
            market_ref_json=excluded.market_ref_json,
            provenance=excluded.provenance, as_of=excluded.as_of
        """,
        {
            "id": row["id"], "brand": row["brand"], "model": row["model"],
            "colorway": row.get("colorway") or "", "sku": row.get("sku"),
            "silhouette": row.get("silhouette") or row["model"], "collab": row.get("collab"),
            "retail_usd": row.get("retail_usd"), "release_date": row.get("release_date"),
            "release_type": row.get("release_type") or "general",
            "region": row.get("region") or "US", "gender": row.get("gender") or "mens",
            "image_url": row.get("image_url"),
            "image_slug": row.get("image_slug"),
            "market_ref_json": json.dumps(row.get("market_ref") or {}),
            "colors_json": json.dumps(row.get("colors") or []),
            "tags_json": json.dumps(row.get("tags") or []),
            "provenance": row.get("provenance") or "seed", "as_of": row.get("as_of") or _now(),
        },
    )


def bootstrap(force: bool = False, today: date | None = None) -> dict[str, Any]:
    """Populate an empty DB from the seed source: real catalog + simulated market."""
    existing = one("SELECT COUNT(*) AS n FROM sneaker")
    if existing and existing["n"] and not force:
        return {"skipped": True, "sneakers": existing["n"]}

    today = today or datetime.now(timezone.utc).date()
    catalog = seed_source.load_catalog()
    upcoming = seed_source.demo_upcoming(catalog, today=today)

    with tx() as conn:
        if force:
            for table in ("heat_score", "size_quote", "news_item", "retailer_link",
                          "market_snapshot", "sneaker"):
                conn.execute(f"DELETE FROM {table}")

        for row in catalog + upcoming:
            upsert_sneaker(conn, row)

        for row in catalog:
            history = seed_source.simulate_history(row, days=120, today=today)
            last_price = history[-1][1]

            # A book on every observation, not just the last: the velocity signal
            # compares a shoe to its own trailing pace, so a single data point
            # would pin it at "exactly average" forever.
            for i, (day, price) in enumerate(history):
                prior = history[max(0, i - 7)][1]
                momentum = ((price - prior) / prior) if prior else 0.0
                book = seed_source.simulate_book(row, price, salt=day, momentum=momentum)
                conn.execute(
                    """INSERT INTO market_snapshot
                       (sneaker_id, ts, last_sale, lowest_ask, highest_bid,
                        sales_72h, bid_count, ask_count, currency, source, is_demo)
                       VALUES (?,?,?,?,?,?,?,?,?,?,1)""",
                    (row["id"], f"{day}T12:00:00+00:00", price,
                     book["lowest_ask"], book["highest_bid"], book["sales_72h"],
                     book["bid_count"], book["ask_count"], "USD", "seed"),
                )

            # One timestamp for the whole size run. Stamping each row separately
            # made `ts = MAX(ts)` select a single size, which silently disabled
            # the size-curve signal for every shoe.
            size_ts = _now()
            for size, quote in seed_source.simulate_sizes(row, last_price).items():
                conn.execute(
                    """INSERT INTO size_quote
                       (sneaker_id, ts, size_us, lowest_ask, last_sale, source, is_demo)
                       VALUES (?,?,?,?,?,?,1)""",
                    (row["id"], size_ts, size, quote["lowest_ask"], quote["last_sale"], "seed"),
                )

            for item in seed_source.simulate_news(row, today=today):
                conn.execute(
                    """INSERT OR REPLACE INTO news_item
                       (id, sneaker_id, ts, source, title, url, weight, is_demo)
                       VALUES (?,?,?,?,?,?,?,1)""",
                    (item["id"], item["sneaker_id"], item["ts"], item["source"],
                     item["title"], item["url"], item["weight"]),
                )

            for link in seed_source.simulate_retailers(row):
                conn.execute(
                    """INSERT INTO retailer_link
                       (sneaker_id, retailer, kind, region, url, opens_at, closes_at, price_usd, is_demo)
                       VALUES (?,?,?,?,?,?,?,?,1)""",
                    (link["sneaker_id"], link["retailer"], link["kind"], link["region"],
                     link["url"], link["opens_at"], link["closes_at"], link["price_usd"]),
                )

        # Upcoming demo drops get buzz and retailers but deliberately no order book.
        for row in upcoming:
            for item in seed_source.simulate_news(row, today=today):
                conn.execute(
                    """INSERT OR REPLACE INTO news_item
                       (id, sneaker_id, ts, source, title, url, weight, is_demo)
                       VALUES (?,?,?,?,?,?,?,1)""",
                    (item["id"], item["sneaker_id"], item["ts"], item["source"],
                     item["title"], item["url"], item["weight"]),
                )
            for link in seed_source.simulate_retailers(row):
                conn.execute(
                    """INSERT INTO retailer_link
                       (sneaker_id, retailer, kind, region, url, opens_at, closes_at, price_usd, is_demo)
                       VALUES (?,?,?,?,?,?,?,?,1)""",
                    (link["sneaker_id"], link["retailer"], link["kind"], link["region"],
                     link["url"], link["opens_at"], link["closes_at"], link["price_usd"]),
                )

        conn.execute(
            """INSERT INTO source_health (name, kind, last_run, last_ok, status, records, detail)
               VALUES ('seed','catalog',?,?, 'ok', ?, 'Offline reference catalog + simulated market.')
               ON CONFLICT(name) DO UPDATE SET last_run=excluded.last_run,
                   last_ok=excluded.last_ok, status='ok', records=excluded.records""",
            (_now(), _now(), len(catalog) + len(upcoming)),
        )

    recompute_heat(today=today)
    return {"skipped": False, "sneakers": len(catalog) + len(upcoming)}


def tick_demo_market(today: date | None = None) -> int:
    """Advance the simulated market by one observation.

    Demo mode still needs to *move*, otherwise 'refreshes every 20 minutes' is an
    untestable claim. Each tick re-runs the simulator with the clock advanced.
    """
    today = today or datetime.now(timezone.utc).date()
    catalog = {r["id"]: r for r in seed_source.load_catalog()}
    written = 0
    with tx() as conn:
        for sneaker_id, row in catalog.items():
            history = seed_source.simulate_history(row, days=8, today=today)
            price = history[-1][1]
            prior = history[0][1]
            momentum = ((price - prior) / prior) if prior else 0.0
            book = seed_source.simulate_book(row, price, salt=_now()[:13], momentum=momentum)
            conn.execute(
                """INSERT INTO market_snapshot
                   (sneaker_id, ts, last_sale, lowest_ask, highest_bid,
                    sales_72h, bid_count, ask_count, currency, source, is_demo)
                   VALUES (?,?,?,?,?,?,?,?, 'USD', 'seed', 1)""",
                (sneaker_id, _now(), price, book["lowest_ask"], book["highest_bid"],
                 book["sales_72h"], book["bid_count"], book["ask_count"]),
            )
            written += 1
    recompute_heat(today=today)
    return written


# --------------------------------------------------------------------------
# live refresh
# --------------------------------------------------------------------------

def _record_health(conn, result: SourceResult, verified: bool) -> None:
    status = "ok" if result.ok else "error"
    detail = result.detail
    if not verified:
        detail = (detail + " " if detail else "") + "[adapter never verified against the live endpoint]"
    conn.execute(
        """INSERT INTO source_health (name, kind, last_run, last_ok, status, latency_ms, records, detail)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(name) DO UPDATE SET
             kind=excluded.kind, last_run=excluded.last_run,
             last_ok=COALESCE(excluded.last_ok, source_health.last_ok),
             status=excluded.status, latency_ms=excluded.latency_ms,
             records=excluded.records, detail=excluded.detail""",
        (result.name, result.kind, _now(), _now() if result.ok else None, status,
         result.latency_ms, result.count, detail),
    )


def _write_records(conn, result: SourceResult) -> None:
    if result.kind == KIND_CATALOG:
        for row in result.records:
            upsert_sneaker(conn, row)
    elif result.kind == KIND_MARKET:
        for row in result.records:
            conn.execute(
                """INSERT INTO market_snapshot
                   (sneaker_id, ts, last_sale, lowest_ask, highest_bid,
                    sales_72h, bid_count, ask_count, currency, source, is_demo)
                   VALUES (?,?,?,?,?,?,?,?,?,?,0)""",
                (row["sneaker_id"], row["ts"], row.get("last_sale"), row.get("lowest_ask"),
                 row.get("highest_bid"), row.get("sales_72h"), row.get("bid_count"),
                 row.get("ask_count"), row.get("currency", "USD"), row["source"]),
            )
    elif result.kind == KIND_NEWS:
        for row in result.records:
            conn.execute(
                """INSERT OR REPLACE INTO news_item
                   (id, sneaker_id, ts, source, title, url, weight, is_demo)
                   VALUES (?,?,?,?,?,?,?,0)""",
                (row["id"], row["sneaker_id"], row["ts"], row["source"],
                 row["title"], row.get("url"), row.get("weight", 1.0)),
            )


def refresh_live(source_names: list[str] | None = None, client: Any = None) -> list[dict[str, Any]]:
    """Run the enabled live sources. Never raises; returns one summary per source."""
    from .live import ALL_SOURCES  # imported lazily so tests can run without httpx

    if not SETTINGS.enable_live_sources:
        return [{"name": "live", "ok": False, "status": "disabled",
                 "detail": "Live sources are off. Set SOLERADAR_ENABLE_LIVE=1 to turn them on."}]

    wanted = set(source_names or SETTINGS.enabled_sources)
    sneakers = [dict(r) for r in query("SELECT * FROM sneaker WHERE provenance != 'demo'")]
    owns_client = client is None
    if owns_client:
        import httpx
        client = httpx.Client(follow_redirects=True, timeout=SETTINGS.http_timeout_s)

    ctx = FetchContext(client=client, sneakers=sneakers, today=datetime.now(timezone.utc).date(),
                       user_agent=SETTINGS.user_agent, timeout_s=SETTINGS.http_timeout_s)
    summaries: list[dict[str, Any]] = []
    try:
        for source in ALL_SOURCES:
            if source.name not in wanted:
                continue
            result = source.fetch(ctx)
            with tx() as conn:
                _write_records(conn, result)
                _record_health(conn, result, verified=False)
            summaries.append({"name": result.name, "kind": result.kind, "ok": result.ok,
                              "records": result.count, "latency_ms": result.latency_ms,
                              "detail": result.detail})
    finally:
        if owns_client:
            client.close()
    recompute_heat()
    return summaries


def warm_images() -> int:
    """Pull product photography into the local cache ahead of anyone asking."""
    import json as _json

    from ..imgcache import warm_all
    rows = []
    for r in query("SELECT * FROM sneaker"):
        d = dict(r)
        d["colors"] = _json.loads(d.get("colors_json") or "[]")
        rows.append(d)
    return warm_all(rows)


# --------------------------------------------------------------------------
# heat recomputation
# --------------------------------------------------------------------------

def _latest_snapshot(sneaker_id: str) -> dict[str, Any] | None:
    row = one(
        """SELECT * FROM market_snapshot
           WHERE sneaker_id=? AND lowest_ask IS NOT NULL
           ORDER BY ts DESC LIMIT 1""", (sneaker_id,))
    if row is None:
        row = one("SELECT * FROM market_snapshot WHERE sneaker_id=? ORDER BY ts DESC LIMIT 1",
                  (sneaker_id,))
    return dict(row) if row else None


def price_series(sneaker_id: str, limit: int = 120) -> list[tuple[str, float]]:
    rows = query(
        """SELECT ts, last_sale FROM market_snapshot
           WHERE sneaker_id=? AND last_sale IS NOT NULL
           ORDER BY ts DESC LIMIT ?""", (sneaker_id, limit))
    return [(r["ts"], r["last_sale"]) for r in reversed(rows)]


def _lineage_score(silhouette: str, exclude_id: str) -> float | None:
    row = one(
        """SELECT AVG(h.score) AS avg_score FROM heat_score h
           JOIN sneaker s ON s.id = h.sneaker_id
           WHERE s.silhouette = ? AND s.id != ? AND s.provenance != 'demo'
             AND h.ts = (SELECT MAX(ts) FROM heat_score WHERE sneaker_id = h.sneaker_id)""",
        (silhouette, exclude_id))
    if row and row["avg_score"] is not None:
        return float(row["avg_score"]) / 100.0
    return None


def compute_for(sneaker: dict[str, Any], today: date | None = None) -> heat_engine.HeatResult:
    sid = sneaker["id"]
    today = today or datetime.now(timezone.utc).date()
    days_out = heat_engine.days_to_release(sneaker.get("release_date"), today=today)
    news_ts = [r["ts"] for r in query(
        "SELECT ts FROM news_item WHERE sneaker_id=? ORDER BY ts DESC LIMIT 40", (sid,))]
    retailer_count = (one("SELECT COUNT(*) AS n FROM retailer_link WHERE sneaker_id=?",
                          (sid,)) or {"n": 0})["n"]
    name = f"{sneaker['brand']} {sneaker['model']} '{sneaker['colorway']}'"

    snap = _latest_snapshot(sid)
    # No order book at all -> this is a pre-release, score it on what exists.
    if snap is None or snap.get("last_sale") is None:
        return heat_engine.pre_release_heat(
            name=name, release_type=sneaker.get("release_type", "general"),
            retailer_count=retailer_count, news_ts=news_ts,
            lineage_score=_lineage_score(sneaker.get("silhouette") or "", sid),
            days_out=days_out,
        )

    series = price_series(sid)
    baseline_row = one(
        """SELECT AVG(sales_72h) AS b FROM (
             SELECT sales_72h FROM market_snapshot
             WHERE sneaker_id=? AND sales_72h IS NOT NULL ORDER BY ts DESC LIMIT 30)""",
        (sid,))
    size_rows = query(
        """SELECT size_us, lowest_ask FROM size_quote
           WHERE sneaker_id=? AND ts=(SELECT MAX(ts) FROM size_quote WHERE sneaker_id=?)""",
        (sid, sid))
    size_asks = {r["size_us"]: r["lowest_ask"] for r in size_rows if r["lowest_ask"]}

    return heat_engine.compute_heat(
        name=name, retail=sneaker.get("retail_usd"), last_sale=snap.get("last_sale"),
        lowest_ask=snap.get("lowest_ask"), highest_bid=snap.get("highest_bid"),
        sales_72h=snap.get("sales_72h"),
        sales_baseline=baseline_row["b"] if baseline_row else None,
        bid_count=snap.get("bid_count"), ask_count=snap.get("ask_count"),
        release_type=sneaker.get("release_type", "general"),
        retailer_count=retailer_count, price_series=series, news_ts=news_ts,
        size_asks=size_asks,
    )


def recompute_heat(today: date | None = None) -> int:
    """Two passes: released shoes first, so pre-release lineage has scores to read."""
    sneakers = [dict(r) for r in query("SELECT * FROM sneaker")]
    released = [s for s in sneakers if s.get("provenance") != "demo"]
    pending = [s for s in sneakers if s.get("provenance") == "demo"]
    written = 0
    for batch in (released, pending):
        rows = []
        for s in batch:
            result = compute_for(s, today=today)
            rows.append((s["id"], _now(), result.score, result.verdict, result.confidence,
                         json.dumps(result.to_dict()["signals"])))
        with tx() as conn:
            conn.executemany(
                """INSERT INTO heat_score (sneaker_id, ts, score, verdict, confidence, signals_json)
                   VALUES (?,?,?,?,?,?)""", rows)
        written += len(rows)
    return written
