"""Refresh cadence.

Three tiers rather than one interval, because the signals decay at different
rates: the order book moves in minutes, the release calendar in days, editorial
coverage in hours. A shoe inside its drop window gets the fast lane.

Deliberately a plain thread rather than APScheduler/Celery: the whole scheduler
is ~70 lines, has no broker, and can be read in one sitting. Swap it out if this
ever needs to survive a process restart mid-job.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger("soleradar.scheduler")


@dataclass
class Job:
    name: str
    interval_s: float
    fn: Callable[[], object]
    last_run: str | None = None
    last_ok: str | None = None
    last_error: str | None = None
    runs: int = 0
    failures: int = 0
    _next_at: float = field(default=0.0, repr=False)

    def due(self, now: float) -> bool:
        return now >= self._next_at

    def schedule_next(self, now: float) -> None:
        self._next_at = now + self.interval_s

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name, "interval_s": self.interval_s,
            "last_run": self.last_run, "last_ok": self.last_ok,
            "last_error": self.last_error, "runs": self.runs, "failures": self.failures,
            "next_in_s": max(0, round(self._next_at - time.monotonic())),
        }


class Scheduler:
    def __init__(self, tick_s: float = 5.0) -> None:
        self.jobs: list[Job] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._tick_s = tick_s

    def add(self, name: str, interval_s: float, fn: Callable[[], object],
            run_at_start: bool = False) -> Job:
        job = Job(name=name, interval_s=interval_s, fn=fn)
        job._next_at = time.monotonic() + (0 if run_at_start else interval_s)
        self.jobs.append(job)
        return job

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="soleradar-scheduler", daemon=True)
        self._thread.start()
        log.info("scheduler started with %d job(s)", len(self.jobs))

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def run_now(self, name: str) -> dict[str, object]:
        for job in self.jobs:
            if job.name == name:
                self._run(job)
                return job.to_dict()
        raise KeyError(name)

    def _run(self, job: Job) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        job.last_run = now_iso
        job.runs += 1
        try:
            job.fn()
            job.last_ok = now_iso
            job.last_error = None
        except Exception as exc:  # noqa: BLE001 - one bad job must not kill the loop
            job.failures += 1
            job.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("job %s failed: %s", job.name, job.last_error)
        finally:
            job.schedule_next(time.monotonic())

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            for job in self.jobs:
                if self._stop.is_set():
                    break
                if job.due(now):
                    self._run(job)
            self._stop.wait(self._tick_s)


SCHEDULER = Scheduler()


def install_default_jobs(scheduler: Scheduler | None = None) -> Scheduler:
    """Wire the standard cadence. Demo mode still ticks, so 'it updates' is a
    claim that can actually be observed without any credentials."""
    from ..config import SETTINGS
    from ..sources import registry

    sch = scheduler or SCHEDULER
    if SETTINGS.enable_live_sources:
        sch.add("market", SETTINGS.market_interval_min * 60,
                lambda: registry.refresh_live(["stockx", "goat"]))
        sch.add("calendar", SETTINGS.calendar_interval_min * 60,
                lambda: registry.refresh_live(["nike"]))
        sch.add("news", SETTINGS.news_interval_min * 60,
                lambda: registry.refresh_live(["rss"]))
    else:
        sch.add("demo-market", SETTINGS.market_interval_min * 60, registry.tick_demo_market)
    sch.add("heat", max(SETTINGS.market_interval_min, 5) * 60, registry.recompute_heat)
    if SETTINGS.fetch_images:
        # Runs once shortly after boot, then hourly to pick up new releases.
        sch.add("images", 3600, registry.warm_images, run_at_start=True)
    return sch
