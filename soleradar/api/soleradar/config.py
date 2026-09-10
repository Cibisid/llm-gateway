"""Runtime configuration.

Everything is env-overridable so the same image runs locally, in Docker and in CI
without code edits. Defaults are chosen so `python -m soleradar` works with no
setup at all -- the app must never *require* a key to start.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # --- storage -----------------------------------------------------------
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("SOLERADAR_DATA_DIR") or REPO_ROOT / "var"))
    seed_dir: Path = field(default_factory=lambda: REPO_ROOT / "data" / "seed")

    # --- server ------------------------------------------------------------
    host: str = os.getenv("SOLERADAR_HOST", "127.0.0.1")
    port: int = _env_int("SOLERADAR_PORT", 8787)

    # --- refresh cadence ---------------------------------------------------
    # Different signals decay at very different rates, so one global interval
    # would either hammer the sources or serve stale prices. See docs/design-decisions.md.
    market_interval_min: int = _env_int("SOLERADAR_MARKET_INTERVAL_MIN", 20)
    calendar_interval_min: int = _env_int("SOLERADAR_CALENDAR_INTERVAL_MIN", 360)
    news_interval_min: int = _env_int("SOLERADAR_NEWS_INTERVAL_MIN", 60)
    # Inside the drop window a release re-prices in seconds, not minutes.
    hot_window_interval_min: int = _env_int("SOLERADAR_HOT_INTERVAL_MIN", 5)
    hot_window_hours: int = _env_int("SOLERADAR_HOT_WINDOW_HOURS", 48)

    # --- network -----------------------------------------------------------
    http_timeout_s: float = float(os.getenv("SOLERADAR_HTTP_TIMEOUT", "12"))
    user_agent: str = os.getenv(
        "SOLERADAR_USER_AGENT",
        "soleradar/0.1 (personal release tracker; +https://github.com/Cibisid/soleradar)",
    )
    # Live sources are opt-in. Off by default so a fresh clone starts in demo
    # mode instead of silently scraping retailers the moment it boots.
    enable_live_sources: bool = _env_bool("SOLERADAR_ENABLE_LIVE", False)
    # Fetching a product photo is not the same act as scraping a price feed, so
    # it is not gated behind enable_live_sources. On by default: the dashboard
    # should show real product photography wherever it can get it.
    fetch_images: bool = _env_bool("SOLERADAR_FETCH_IMAGES", True)
    image_workers: int = _env_int("SOLERADAR_IMAGE_WORKERS", 4)

    enabled_sources: tuple[str, ...] = tuple(
        s.strip() for s in os.getenv("SOLERADAR_SOURCES", "stockx,goat,nike,rss").split(",") if s.strip()
    )

    # --- economics ---------------------------------------------------------
    default_region: str = os.getenv("SOLERADAR_REGION", "US")
    currency: str = os.getenv("SOLERADAR_CURRENCY", "USD")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "soleradar.sqlite3"

    @property
    def image_cache_dir(self) -> Path:
        return self.data_dir / "images"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.image_cache_dir.mkdir(parents=True, exist_ok=True)


SETTINGS = Settings()
