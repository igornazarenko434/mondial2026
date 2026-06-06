"""Scheduler daemon — the always-on loop that runs the system.

Every `poll_seconds` it: refreshes fixtures, finds jobs due in this window
(T-24h/-60m/-15m/-7m), and dispatches each to a ThreadPoolExecutor so
**simultaneous kickoffs run concurrently**. Writes a heartbeat and runs the
watchdog each tick.

Concurrency choice (best practice): the work is **I/O-bound** (API/odds/LLM
calls) — the model math is microseconds — so **threads** are correct, not
multiprocessing (which suits CPU-bound work and adds overhead). Python releases
the GIL during I/O, so two match pipelines truly overlap. The shared token-bucket
rate limiter (thread-safe) keeps concurrent jobs within free-tier limits.

Run it: `python -m schedule.runner` (put it under systemd/launchd so the OS
restarts it if it dies — see docs/SCHEDULING.md).
"""
from __future__ import annotations
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable
from schedule.scheduler import due_jobs
from schedule import watchdog
from orchestrator.pipeline import process_match
from core.obs.logging import get_logger
from core.obs.runs import runs
from core import obs

log = get_logger("scheduler")


class SchedulerDaemon:
    def __init__(self, fixtures_fn: Callable[[], list[dict]],
                 build_card: Callable[[dict], dict],
                 ingest_fn: Callable[[], None] | None = None,
                 max_workers: int | None = None, poll_seconds: int | None = None,
                 ingest_every_min: int | None = None):
        self.fixtures_fn = fixtures_fn           # reads upcoming matches (store.repo)
        self.build_card = build_card
        self.ingest_fn = ingest_fn               # refreshes the calendar/results/bracket
        self.max_workers = max_workers or int(os.environ.get("SCHED_MAX_WORKERS", "4"))
        self.poll_seconds = poll_seconds or int(os.environ.get("SCHED_POLL_SECONDS", "60"))
        self.ingest_every_min = ingest_every_min or int(os.environ.get("INGEST_EVERY_MIN", "30"))
        self.pool = ThreadPoolExecutor(max_workers=self.max_workers,
                                       thread_name_prefix="match")
        self._dispatched: set[tuple] = set()   # idempotency: never run a job twice
        self._last_ingest: datetime | None = None

    def _run_job(self, match: dict, window: str):
        # each job is fully isolated; failures are handled inside process_match.
        # Stamp the window onto the match dict so build_card knows which window
        # it's serving without process_match changing signature.
        match = {**match, "_window": window}
        return process_match(match, window, self.build_card)

    def _maybe_ingest(self, now: datetime):
        """Periodically refresh fixtures/results/bracket from the source."""
        if not self.ingest_fn:
            return
        if (self._last_ingest is None
                or (now - self._last_ingest).total_seconds() >= self.ingest_every_min * 60):
            try:
                self.ingest_fn()
                self._last_ingest = now
                log.info("calendar refreshed (fixtures/results/bracket)")
            except Exception as e:  # noqa: BLE001
                log.warning("ingest failed (will retry next cycle): %s", e)

    def tick(self, now: datetime | None = None) -> list[tuple]:
        now = now or datetime.now(timezone.utc)
        self._maybe_ingest(now)
        matches = self.fixtures_fn()
        by_id = {m["match_id"]: m for m in matches}
        submitted = []
        # persistent idempotency (survives restarts) + in-memory fast path
        for job in due_jobs(matches, now, is_done=lambda mid, w: runs().was_handled(mid, w)):
            key = (job["match_id"], job["window"])
            if key in self._dispatched:
                continue
            self._dispatched.add(key)
            self.pool.submit(self._run_job, by_id[job["match_id"]], job["window"])
            submitted.append(key)
        if submitted:
            log.info("dispatched %d job(s): %s", len(submitted), submitted)
        watchdog.beat()
        watchdog.run_checks()
        return submitted

    def run_forever(self):
        obs.setup()
        from config.preflight import check
        check()                              # surface misconfig loudly at startup
        log.info("scheduler started (workers=%d, poll=%ds)", self.max_workers, self.poll_seconds)
        try:
            while True:
                try:
                    self.tick()
                except Exception as e:  # noqa: BLE001 - loop must never die on one bad tick
                    log.error("tick error: %s", e)
                time.sleep(self.poll_seconds)
        finally:
            self.pool.shutdown(wait=True)


if __name__ == "__main__":
    # Live wiring (Day 6): fixtures come from SQLite (refreshed by
    # football_data.refresh), build_card is the REAL Day-6 assembler.
    from store.db import connect, init_db
    from store import repo
    from core.data import football_data
    from core.decision.build_card import build_card as real_build_card

    init_db()
    conn = connect()

    def fixtures():
        return repo.upcoming_matches(conn)

    def ingest():
        football_data.refresh(conn)       # calendar + results + bracket + detonator tags

    def build(match):
        # Persist to the predictions table on the same connection used for
        # fixture reads. The window comes from the scheduler dispatch
        # context (set on the match dict in _run_job — see below).
        return real_build_card(match, conn=conn,
                               window=match.get("_window", "T-7m"))

    SchedulerDaemon(fixtures, build, ingest_fn=ingest).run_forever()
