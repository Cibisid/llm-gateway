"""The offline source: real reference catalog + a deterministic market simulator.

Two very different kinds of data live here and they are labelled differently on
purpose:

  * catalog facts (brand, silhouette, SKU, retail, original release date) are
    compiled reference values -- provenance "reference";
  * every price, bid, ask, sale count and upcoming-drop date is SIMULATED --
    provenance "demo", is_demo=1.

The simulator exists so the dashboard is fully functional with no credentials and
no network. It is seeded by shoe id, so the same clone always produces the same
market and screenshots/tests are reproducible. It is not a forecast of anything.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from ..config import SETTINGS

SIZES = [4.0, 5.0, 6.0, 6.5, 7.0, 7.5, 8.0, 8.5, 9.0, 9.5, 10.0, 10.5,
         11.0, 11.5, 12.0, 13.0, 14.0, 15.0]

RETAILERS = {
    "general": [("Nike / SNKRS", "fcfs"), ("Foot Locker", "fcfs"), ("JD Sports", "fcfs"),
                ("Finish Line", "fcfs"), ("Hibbett", "fcfs")],
    "limited": [("Nike / SNKRS", "draw"), ("END.", "raffle"), ("Size?", "raffle"),
                ("Sneakersnstuff", "raffle")],
    "collab": [("Nike / SNKRS", "draw"), ("END.", "raffle"), ("Dover Street Market", "raffle")],
    "numbered": [("Brand direct", "draw")],
    "friends_family": [("Not sold at retail", "in_store")],
}

# Archetype -> (low, high) resale multiple of retail. Wide bands on purpose: the
# point of the demo market is that shoes land in genuinely different regimes.
ARCHETYPE_MULTIPLE: dict[str, tuple[float, float]] = {
    "grail": (2.6, 6.5),
    "collab": (1.6, 3.4),
    "limited": (1.05, 2.1),
    "trend": (1.0, 1.7),
    "general": (0.72, 1.25),
}

NEWS_SOURCES = ["Sneaker News", "Hypebeast", "Highsnobiety", "Complex Sneakers",
                "House of Heat", "Sole Retriever"]

NEWS_TEMPLATES = [
    "Official images of the {name}",
    "{name} release date confirmed",
    "Where to buy the {name}",
    "The {name} is restocking",
    "First look: {name}",
    "{name} resale is climbing",
]


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _rng(*parts: str) -> random.Random:
    """Deterministic per-entity RNG: same repo, same numbers, every time."""
    digest = hashlib.sha256("::".join(parts).encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def load_catalog() -> list[dict[str, Any]]:
    path = SETTINGS.seed_dir / "catalog.json"
    with path.open(encoding="utf-8") as fh:
        doc = json.load(fh)
    schema = doc["schema"]
    rows = [dict(zip(schema, row)) for row in doc["rows"]]
    for r in rows:
        r["provenance"] = doc.get("provenance", "reference")
        r["as_of"] = doc.get("as_of")
        r["region"] = "US"
        r["gender"] = "mens"
        # image_url is left unset: imgcache builds a candidate chain from
        # image_slug, which is a real StockX slug rather than a guess, and falls
        # back to generated art whenever every candidate 404s or is blocked.
        r.setdefault("image_url", None)
        r.setdefault("image_slug", None)
        r.setdefault("market_ref", None)
    return rows


def archetype_of(row: dict[str, Any]) -> str:
    tags = set(row.get("tags") or [])
    if "grail" in tags:
        return "grail"
    rt = row.get("release_type", "general")
    if rt in {"collab", "numbered", "friends_family"}:
        return "collab"
    if rt == "limited":
        return "limited"
    if "trend" in tags:
        return "trend"
    return "general"


def _phase_factor(days_since: int) -> float:
    """The classic post-release curve, as a multiplier on the shoe's base level.

    Peak in week one, trough around week 10, slow recovery afterwards.
    """
    if days_since < 0:
        # Pre-release asks run hot and firm up as the date approaches.
        return 1.10 + min(abs(days_since), 120) / 1200.0
    if days_since <= 7:
        return 1.18 - 0.01 * days_since
    if days_since <= 70:
        # Flipper dump: glide down to ~0.80 of base.
        t = (days_since - 7) / 63.0
        return 1.11 - 0.31 * t
    if days_since <= 540:
        t = (days_since - 70) / 470.0
        return 0.80 + 0.30 * t
    return min(1.10 + (days_since - 540) / 3650.0 * 0.6, 2.2)


def simulate_history(row: dict[str, Any], days: int = 120,
                     today: date | None = None) -> list[tuple[str, float]]:
    """A price series that ENDS at the shoe's real reference level.

    The first version drew a random multiple of retail per archetype, which made
    a Panda Dunk and a Travis SB look like the same kind of asset scaled up. Now
    the catalog carries an approximate real resale band per shoe, and the series
    is generated with a realistic SHAPE (release-phase curve plus a random walk)
    and then rescaled so today's value lands on that band's typical price.

    So: the level is real, the movement is synthetic. That split is the honest
    one -- nobody can reconstruct a true daily history offline, but anchoring to
    a real level means a shoe that trades under retail looks like it trades under
    retail, which is most of what makes the dashboard worth reading.
    """
    today = today or datetime.now(timezone.utc).date()
    rng = _rng(row["id"], "history")
    arche = archetype_of(row)
    retail = row.get("retail_usd") or 120.0

    ref = row.get("market_ref") or {}
    anchor = ref.get("typical")
    if anchor:
        # Band width is a real observation about the shoe: a wide band means a
        # volatile market, so let it drive the noise rather than the archetype.
        #
        # Converting a ~12-month range into a DAILY sigma needs the right scale.
        # A random walk's yearly range spans roughly 4 annual sigma, and a year is
        # ~252 trading days, so daily = span / (4 * sqrt(252)) ~= span / 63.
        # Dividing by 7 (the first attempt) implied ~4.5%/day and printed 34%
        # weekly swings on shoes that move a few percent in reality.
        span = (ref.get("high", anchor) - ref.get("low", anchor)) / anchor
        vol = _clamp(span / 63.0, 0.003, 0.022)
    else:
        lo, hi = ARCHETYPE_MULTIPLE[arche]
        anchor = retail * rng.uniform(lo, hi)
        vol = {"grail": 0.030, "collab": 0.026, "limited": 0.021,
               "trend": 0.017, "general": 0.012}[arche]

    try:
        released = date.fromisoformat(row["release_date"][:10]) if row.get("release_date") else None
    except (ValueError, TypeError):
        released = None

    drift = rng.uniform(-0.0022, 0.0026)

    raw: list[float] = []
    level = 1.0
    for i in range(days, -1, -1):
        d = today - timedelta(days=i)
        days_since = (d - released).days if released else 400
        level *= math.exp(drift + rng.gauss(0, vol))
        raw.append(_phase_factor(days_since) * level)

    # Rescale so the last observation is the real reference price.
    scale = anchor / raw[-1] if raw[-1] else anchor
    series: list[tuple[str, float]] = []
    for i, factor in enumerate(raw):
        d = today - timedelta(days=days - i)
        price = factor * scale
        series.append((d.isoformat(), round(max(price, retail * 0.25), 2)))
    return series


def simulate_book(row: dict[str, Any], last_sale: float, salt: str = "",
                  momentum: float = 0.0) -> dict[str, Any]:
    """Order book for one observation.

    `momentum` is recent price change. Sales volume and bid depth track it on
    purpose: in a real market a shoe whose price is running also trades more
    often, and a book that ignored that would make the velocity signal inert.
    """
    rng = _rng(row["id"], "book", salt)
    arche = archetype_of(row)
    # Liquid shoes quote tight; dead stock quotes wide.
    spread = {"grail": 0.07, "collab": 0.09, "limited": 0.13, "trend": 0.16, "general": 0.22}[arche]
    ref = row.get("market_ref") or {}
    if ref.get("typical"):
        # A wide observed price band is itself evidence of a loose book.
        span = (ref.get("high", 0) - ref.get("low", 0)) / ref["typical"]
        spread = _clamp((spread + span / 3.0) / 2.0, 0.04, 0.30)
    spread *= rng.uniform(0.85, 1.20)
    lowest_ask = last_sale * (1 + spread * 0.55)
    highest_bid = last_sale * (1 - spread * 0.45)

    popularity = {"grail": 260, "collab": 180, "limited": 90, "trend": 70, "general": 45}[arche]
    lift = 1.0 + 2.4 * max(-0.45, min(0.45, momentum))
    sales_72h = max(1, int(rng.gauss(popularity * lift, popularity * 0.28)))

    # Scarce shoes sit bid-heavy (buyers queueing), general releases ask-heavy
    # (sellers queueing). Centring both bands on 1.0 made heat and demand
    # pressure disagree on the same shoe.
    demand_tilt = {"grail": (1.1, 2.6), "collab": (0.95, 2.2), "limited": (0.7, 1.6),
                   "trend": (0.5, 1.25), "general": (0.22, 0.85)}[arche]
    demand_tilt = rng.uniform(*demand_tilt)
    demand_tilt *= 1.0 + 1.6 * max(-0.4, min(0.4, momentum))
    ask_count = max(3, int(rng.gauss(popularity * 1.4, popularity * 0.4)))
    bid_count = max(1, int(ask_count * demand_tilt))
    return {
        "last_sale": round(last_sale, 2),
        "lowest_ask": round(lowest_ask, 2),
        "highest_bid": round(highest_bid, 2),
        "sales_72h": sales_72h,
        "bid_count": bid_count,
        "ask_count": ask_count,
    }


def simulate_sizes(row: dict[str, Any], last_sale: float) -> dict[float, dict[str, float]]:
    """Per-size asks.

    Healthy demand bids UP the core sizes relative to the tails; a shoe that is
    sitting inverts that, because the tails were produced in smaller numbers and
    end up the only scarce thing about it.
    """
    rng = _rng(row["id"], "sizes")
    arche = archetype_of(row)
    healthy = arche in {"grail", "collab", "limited"}
    out: dict[float, dict[str, float]] = {}
    for s in SIZES:
        # Distance from the middle of the core run.
        dist = abs(s - 9.75) / 5.75
        # Tails cost more on almost every shoe -- fewer pairs made, fewer listed.
        # What varies is how EXTREME the skew is: on a shoe that is sitting, the
        # tails are the only place any premium survives.
        factor = 1.0 + (0.07 if healthy else 0.30) * dist
        factor *= rng.uniform(0.96, 1.05)
        ask = last_sale * factor * rng.uniform(1.02, 1.12)
        out[s] = {"lowest_ask": round(ask, 2), "last_sale": round(ask * rng.uniform(0.88, 0.98), 2)}
    return out


def simulate_news(row: dict[str, Any], today: date | None = None) -> list[dict[str, Any]]:
    today = today or datetime.now(timezone.utc).date()
    rng = _rng(row["id"], "news")
    arche = archetype_of(row)
    count = {"grail": 9, "collab": 7, "limited": 4, "trend": 3, "general": 1}[arche]
    count = max(0, int(rng.gauss(count, 1.6)))
    name = f"{row['brand']} {row['model']} '{row['colorway']}'"
    items = []
    for i in range(count):
        age = rng.uniform(0, 14)
        ts = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=age)
        items.append({
            "id": f"{row['id']}-news-{i}",
            "sneaker_id": row["id"],
            "ts": ts.isoformat(),
            "source": rng.choice(NEWS_SOURCES),
            "title": rng.choice(NEWS_TEMPLATES).format(name=name),
            "url": None,
            "weight": 1.0,
        })
    return items


def simulate_retailers(row: dict[str, Any]) -> list[dict[str, Any]]:
    rng = _rng(row["id"], "retail")
    pool = RETAILERS.get(row.get("release_type", "general"), RETAILERS["general"])
    n = rng.randint(1, len(pool))
    picks = rng.sample(pool, n)
    out = []
    for name, kind in picks:
        out.append({
            "sneaker_id": row["id"], "retailer": name, "kind": kind, "region": "US",
            "url": None, "opens_at": None, "closes_at": None,
            "price_usd": row.get("retail_usd"),
        })
    return out


# --------------------------------------------------------------------------
# demo upcoming calendar
# --------------------------------------------------------------------------

# Colourway names for simulated future drops. These releases DO NOT EXIST -- they
# exist so the calendar view has something to render before a live source is
# connected, and they are stamped provenance="demo" everywhere they surface.
DEMO_COLORWAYS = [
    ("Sail/Deep Ocean", ["#EFEADC", "#243A5E", "#C7C2B4"]),
    ("Cacao Wow", ["#6B4A38", "#E5D8C4", "#2E2119"]),
    ("Vintage Green", ["#4E6B4F", "#EFE9D9", "#2A3A2A"]),
    ("Metallic Gold", ["#C8A24A", "#F2EEE3", "#2A2A2A"]),
    ("Triple Grey", ["#9A9A97", "#C6C5C0", "#6E6E6B"]),
    ("Varsity Royal", ["#2B4EA2", "#F1EFE8", "#1B1B1B"]),
    ("Desert Ore", ["#C9A882", "#EFE7DA", "#4A3A2C"]),
    ("Court Purple", ["#5B2C83", "#F0EDE4", "#1C1C1C"]),
]


def demo_upcoming(catalog: list[dict[str, Any]], count: int = 14,
                  today: date | None = None) -> list[dict[str, Any]]:
    today = today or datetime.now(timezone.utc).date()
    rng = _rng("upcoming", today.isoformat())
    # Draw from silhouettes that actually carry resale, so the calendar is varied.
    pool = [r for r in catalog if archetype_of(r) in {"grail", "collab", "limited", "trend"}]
    rng.shuffle(pool)
    out: list[dict[str, Any]] = []
    for i in range(min(count, len(pool))):
        base = pool[i]
        cw, colors = DEMO_COLORWAYS[i % len(DEMO_COLORWAYS)]
        drop_in = rng.randint(2, 88)
        out.append({
            "id": f"demo-upcoming-{base['id']}-{i}",
            "brand": base["brand"],
            "model": base["model"],
            "colorway": cw,
            "sku": None,
            "silhouette": base["silhouette"],
            "collab": base.get("collab"),
            "retail_usd": base.get("retail_usd"),
            "release_date": (today + timedelta(days=drop_in)).isoformat(),
            "release_type": base.get("release_type", "limited"),
            "region": "US",
            "gender": "mens",
            "colors": colors,
            "tags": sorted(set((base.get("tags") or []) + ["upcoming"])),
            "image_url": None,
            "provenance": "demo",
            "as_of": today.isoformat(),
        })
    return out


def iter_all(today: date | None = None) -> Iterable[dict[str, Any]]:
    catalog = load_catalog()
    yield from catalog
    yield from demo_upcoming(catalog, today=today)
