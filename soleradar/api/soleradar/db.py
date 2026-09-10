"""SQLite storage.

Plain sqlite3 rather than an ORM: the schema is small, the queries are the
interesting part, and a single-file DB means "clone and run" with no service to
stand up. WAL is on so the background scheduler can write while the API reads.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import SETTINGS

_LOCAL = threading.local()

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Reference catalog: brand, silhouette, SKU, retail, release date.
-- Slow-moving, human-verifiable facts. Kept apart from market data on purpose.
CREATE TABLE IF NOT EXISTS sneaker (
    id            TEXT PRIMARY KEY,
    brand         TEXT NOT NULL,
    model         TEXT NOT NULL,
    colorway      TEXT NOT NULL,
    sku           TEXT,
    silhouette    TEXT NOT NULL,
    collab        TEXT,
    retail_usd    REAL,
    release_date  TEXT,              -- ISO date; NULL when unannounced
    release_type  TEXT NOT NULL,     -- general | limited | collab | numbered | friends_family
    region        TEXT NOT NULL DEFAULT 'US',
    gender        TEXT NOT NULL DEFAULT 'mens',
    image_url     TEXT,
    -- The real StockX product slug. Its CDN path is derivable from this, which is
    -- why it is stored rather than guessed from the model name at request time:
    -- naive slugification of "Air Jordan 1 Retro High OG / Chicago Lost and Found"
    -- does not produce the slug StockX actually uses.
    image_slug    TEXT,
    -- Whichever candidate URL resolved. Cached so we stop re-trying dead ones.
    image_resolved TEXT,
    -- The offline-compiled resale band this shoe's demo prices are anchored to,
    -- carrying its own confidence. Stored so the UI can show how firm a number is.
    market_ref_json TEXT NOT NULL DEFAULT '{}',
    colors_json   TEXT NOT NULL DEFAULT '[]',
    tags_json     TEXT NOT NULL DEFAULT '[]',
    provenance    TEXT NOT NULL DEFAULT 'seed',
    as_of         TEXT
);
CREATE INDEX IF NOT EXISTS idx_sneaker_release ON sneaker(release_date);
CREATE INDEX IF NOT EXISTS idx_sneaker_brand   ON sneaker(brand);

-- One row per observation of the aggregate market for a shoe.
-- is_demo is never inferred at read time -- it is stamped by whoever wrote the row.
CREATE TABLE IF NOT EXISTS market_snapshot (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sneaker_id   TEXT NOT NULL REFERENCES sneaker(id) ON DELETE CASCADE,
    ts           TEXT NOT NULL,
    last_sale    REAL,
    lowest_ask   REAL,
    highest_bid  REAL,
    sales_72h    INTEGER,
    bid_count    INTEGER,
    ask_count    INTEGER,
    currency     TEXT NOT NULL DEFAULT 'USD',
    source       TEXT NOT NULL,
    is_demo      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_snap_sneaker_ts ON market_snapshot(sneaker_id, ts DESC);

-- Per-size asks. The size curve is where "hot" and "sitting" actually separate:
-- a shoe carrying premium only at 7 and 14 is not in demand, it is thin.
CREATE TABLE IF NOT EXISTS size_quote (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sneaker_id  TEXT NOT NULL REFERENCES sneaker(id) ON DELETE CASCADE,
    ts          TEXT NOT NULL,
    size_us     REAL NOT NULL,
    lowest_ask  REAL,
    last_sale   REAL,
    source      TEXT NOT NULL,
    is_demo     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_size_sneaker_ts ON size_quote(sneaker_id, ts DESC);

-- Editorial / social mentions, used as the buzz signal.
CREATE TABLE IF NOT EXISTS news_item (
    id          TEXT PRIMARY KEY,
    sneaker_id  TEXT REFERENCES sneaker(id) ON DELETE CASCADE,
    ts          TEXT NOT NULL,
    source      TEXT NOT NULL,
    title       TEXT NOT NULL,
    url         TEXT,
    weight      REAL NOT NULL DEFAULT 1.0,
    is_demo     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_news_sneaker_ts ON news_item(sneaker_id, ts DESC);

-- Where to actually buy it, and under what mechanism.
CREATE TABLE IF NOT EXISTS retailer_link (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sneaker_id  TEXT NOT NULL REFERENCES sneaker(id) ON DELETE CASCADE,
    retailer    TEXT NOT NULL,
    kind        TEXT NOT NULL,     -- raffle | draw | fcfs | shock | in_store
    region      TEXT NOT NULL DEFAULT 'US',
    url         TEXT,
    opens_at    TEXT,
    closes_at   TEXT,
    price_usd   REAL,
    is_demo     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_retailer_sneaker ON retailer_link(sneaker_id);

-- Computed, not fetched. Stored so we can chart how heat itself moved.
CREATE TABLE IF NOT EXISTS heat_score (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sneaker_id   TEXT NOT NULL REFERENCES sneaker(id) ON DELETE CASCADE,
    ts           TEXT NOT NULL,
    score        REAL NOT NULL,
    verdict      TEXT NOT NULL,
    confidence   REAL NOT NULL DEFAULT 0,
    signals_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_heat_sneaker_ts ON heat_score(sneaker_id, ts DESC);

CREATE TABLE IF NOT EXISTS portfolio_lot (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sneaker_id  TEXT NOT NULL REFERENCES sneaker(id) ON DELETE CASCADE,
    size_us     REAL,
    qty         INTEGER NOT NULL DEFAULT 1,
    cost_usd    REAL NOT NULL,
    bought_at   TEXT NOT NULL,
    sold_at     TEXT,
    sold_usd    REAL,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS watch (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sneaker_id  TEXT NOT NULL REFERENCES sneaker(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL DEFAULT 'price',   -- price | heat | drop
    direction   TEXT NOT NULL DEFAULT 'below',   -- below | above
    threshold   REAL,
    created_at  TEXT NOT NULL,
    triggered_at TEXT,
    UNIQUE(sneaker_id, kind, direction, threshold)
);

-- Per-source health so the UI can tell the truth about where numbers came from.
CREATE TABLE IF NOT EXISTS source_health (
    name         TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    last_run     TEXT,
    last_ok      TEXT,
    status       TEXT NOT NULL DEFAULT 'idle',   -- ok | error | disabled | idle
    latency_ms   INTEGER,
    records      INTEGER NOT NULL DEFAULT 0,
    detail       TEXT
);
"""


def connect() -> sqlite3.Connection:
    """One connection per thread; sqlite3 objects are not thread-safe to share."""
    conn = getattr(_LOCAL, "conn", None)
    if conn is None:
        SETTINGS.ensure_dirs()
        conn = sqlite3.connect(SETTINGS.db_path, timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        _LOCAL.conn = conn
    return conn


def init_db(path: Path | None = None) -> None:
    conn = connect() if path is None else sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def query(sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
    return connect().execute(sql, params).fetchall()


def one(sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
    return connect().execute(sql, params).fetchone()


def row_to_dict(row: sqlite3.Row | None, json_fields: tuple[str, ...] = ()) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for f in json_fields:
        if f in out and isinstance(out[f], str):
            try:
                out[f] = json.loads(out[f])
            except json.JSONDecodeError:
                out[f] = []
    return out
