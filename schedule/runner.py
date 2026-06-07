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

Day-9 additions (all idempotent, all fail-safe):
  • events_cache batching — ONE fetch_all_odds per tick is shared across every
    match in that tick (was N HTTP calls; cuts ~95% of the odds_api quota burn
    during the tournament).
  • Auto-standings — after each ingest, update_standings() runs so the
    standings table is always ≤30 min stale with no manual command needed.
  • Daily summary — once per day at 09:00 (configurable TZ) the daemon pushes
    a Telegram message with today's games + recent results + your score +
    budget. Doubles as a positive heartbeat: if it stops arriving, the daemon
    is down even when alerts also fail.
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


# Day-9: windows where odds_api is queried in build_card → batch-fetch them.
# T-24h is a news/preview-only window (no odds pull), so it's excluded.
ODDS_WINDOWS = ("T-60m", "T-15m", "T-7m")


class SchedulerDaemon:
    def __init__(self, fixtures_fn: Callable[[], list[dict]],
                 build_card: Callable[[dict], dict],
                 ingest_fn: Callable[[], None] | None = None,
                 events_cache_fn: Callable[[], list] | None = None,
                 standings_update_fn: Callable[[], None] | None = None,
                 daily_summary_fn: Callable[..., bool] | None = None,
                 strategy_context_fn: Callable[[], dict | None] | None = None,
                 strategy_tilt: float | None = None,
                 max_workers: int | None = None, poll_seconds: int | None = None,
                 ingest_every_min: int | None = None):
        self.fixtures_fn = fixtures_fn           # reads upcoming matches (store.repo)
        self.build_card = build_card
        self.ingest_fn = ingest_fn               # refreshes the calendar/results/bracket
        # Day-9: 3 NEW optional hooks. All None by default → existing tests +
        # call sites untouched. The runner.__main__ wires the live versions.
        self.events_cache_fn = events_cache_fn   # () -> list[event_dict] | raises
        self.standings_update_fn = standings_update_fn   # () -> None (idempotent)
        self.daily_summary_fn = daily_summary_fn          # (*, now) -> bool sent?
        # Day-9.5 win-the-pool layer — both None by default so the daemon
        # produces pure-EV picks. Pass both to enable position-aware tilting.
        # See docs/STRATEGY.md.
        self.strategy_context_fn = strategy_context_fn    # () -> ctx dict | None
        self.strategy_tilt = strategy_tilt                # float in [0, 1] or None
        # Workers default bumped from 4 → 6 (CLAUDE.md Day-9 note): the WC
        # group stage has up to 4 simultaneous kickoffs per slot, and at least
        # 2 spare threads cover slow Brave/LLM calls without starving siblings.
        self.max_workers = max_workers or int(os.environ.get("SCHED_MAX_WORKERS", "6"))
        self.poll_seconds = poll_seconds or int(os.environ.get("SCHED_POLL_SECONDS", "60"))
        self.ingest_every_min = ingest_every_min or int(os.environ.get("INGEST_EVERY_MIN", "30"))
        self.pool = ThreadPoolExecutor(max_workers=self.max_workers,
                                       thread_name_prefix="match")
        self._dispatched: set[tuple] = set()   # idempotency: never run a job twice
        self._last_ingest: datetime | None = None

    def _run_job(self, match: dict, window: str, events_cache=None):
        # each job is fully isolated; failures are handled inside process_match.
        # Stamp the window AND the per-tick events_cache onto the match dict so
        # build_card can use them without process_match changing signature.
        match = {**match, "_window": window, "_events_cache": events_cache}
        # Day-9.5: load standings context at DISPATCH time (not at startup),
        # so any standings_set update during the tournament takes effect on
        # the next match without restarting the daemon. None ⇒ no tilt.
        ctx = None
        if self.strategy_context_fn:
            try:
                ctx = self.strategy_context_fn()
            except Exception as e:                 # noqa: BLE001 — never break a card
                log.warning("strategy_context_fn failed: %s; using pure-EV", e)
                ctx = None
        return process_match(match, window, self.build_card,
                              strategy_context=ctx,
                              strategy_tilt=self.strategy_tilt)

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

    def _maybe_update_standings(self):
        """Day-9: re-score every finished match. Idempotent; cheap (pure SQL +
        a deterministic score_match). Runs each tick so the standings table is
        always current within one poll cycle of a final whistle."""
        if not self.standings_update_fn:
            return
        try:
            self.standings_update_fn()
        except Exception as e:               # noqa: BLE001 — scoring must never crash the loop
            log.warning("standings update failed: %s", e)

    def _maybe_daily_summary(self, now: datetime):
        """Day-9: 09:00-local positive heartbeat + day-at-a-glance."""
        if not self.daily_summary_fn:
            return
        try:
            if self.daily_summary_fn(now=now):
                log.info("daily summary sent")
        except Exception as e:               # noqa: BLE001
            log.warning("daily summary failed: %s", e)

    def _fetch_events_cache_if_needed(self, due: list[dict]) -> list | None:
        """Day-9: one fetch_all_odds() per tick is shared across every match
        whose window pulls odds (T-60m / T-15m / T-7m). On any failure we
        return None and build_card falls back to its per-match path — never
        breaks the tick."""
        if not self.events_cache_fn:
            return None
        if not any(j["window"] in ODDS_WINDOWS for j in due):
            return None
        try:
            ec = self.events_cache_fn()
            log.info("events_cache fetched once for tick: %d events",
                     len(ec) if ec is not None else -1)
            return ec
        except Exception as e:               # noqa: BLE001
            log.warning("events_cache fetch failed: %s; per-match fallback", e)
            return None

    def tick(self, now: datetime | None = None) -> list[tuple]:
        now = now or datetime.now(timezone.utc)
        self._maybe_ingest(now)
        self._maybe_update_standings()                # Day-9: post-ingest scoring
        self._maybe_daily_summary(now)                # Day-9: morning summary
        matches = self.fixtures_fn()
        by_id = {m["match_id"]: m for m in matches}
        # persistent idempotency (survives restarts) + in-memory fast path
        due = list(due_jobs(matches, now,
                            is_done=lambda mid, w: runs().was_handled(mid, w)))
        events_cache = self._fetch_events_cache_if_needed(due)  # Day-9: batch
        submitted = []
        for job in due:
            key = (job["match_id"], job["window"])
            if key in self._dispatched:
                continue
            self._dispatched.add(key)
            self.pool.submit(self._run_job, by_id[job["match_id"]],
                             job["window"], events_cache)
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
    # Live wiring (Day 6 + Day 9): fixtures come from SQLite (refreshed by
    # football_data.refresh), build_card is the REAL Day-6 assembler, and the
    # three Day-9 hooks (events_cache, standings, daily summary) are wired
    # against the same shared SQLite connection.
    from store.db import connect, init_db
    from store import repo
    from core.data import football_data
    from core.data.oddsapi import fetch_all_odds
    from core.decision.build_card import build_card as real_build_card
    from core.scoring.standings_writer import update_standings
    from schedule.daily_summary import send_if_due as _send_daily_summary
    from config.strategy import DEFAULT_TILT

    init_db()
    conn = connect()

    def fixtures():
        return repo.upcoming_matches(conn)

    def ingest():
        football_data.refresh(conn)       # calendar + results + bracket + detonator tags

    def build(match):
        # Persist to the predictions table on the same connection used for
        # fixture reads. window + events_cache come from the scheduler
        # dispatch context (stamped on the match dict in _run_job).
        return real_build_card(match, conn=conn,
                               window=match.get("_window", "T-7m"),
                               events_cache=match.get("_events_cache"))

    def events_cache_fetcher():
        # ONE batch call per tick → fetch_all_odds returns every WC event;
        # build_card finds its own match inside that list. Free for the
        # the-odds-api batch endpoint (1 credit per call regardless of N events).
        return fetch_all_odds(regions="eu,uk", markets="h2h")

    # MY_PARTICIPANT defaults to "me" for backwards-compat with the existing
    # single-row tests + earlier deploys. Set this in .env to match your
    # display name in the Negev Toto app once you turn the win-the-pool tilt
    # on. Used by BOTH the writer (so update_standings tags YOUR row) and
    # the reader (so standings_context computes the right gap).
    my_participant = os.environ.get("MY_PARTICIPANT", "me")

    def standings_updater():
        update_standings(conn, participant=my_participant)

    def daily_summary_sender(*, now):
        return _send_daily_summary(conn, runs(), now=now)

    def strategy_context_loader():
        # Re-read on each dispatch. Cheap (two SELECTs). Means a fresh
        # `tools/standings_set.py set ...` is picked up by the very next
        # match-window job — no daemon restart needed.
        from store import repo
        return repo.standings_context(conn, me=my_participant)

    SchedulerDaemon(fixtures, build, ingest_fn=ingest,
                    events_cache_fn=events_cache_fetcher,
                    standings_update_fn=standings_updater,
                    daily_summary_fn=daily_summary_sender,
                    strategy_context_fn=strategy_context_loader,
                    strategy_tilt=DEFAULT_TILT).run_forever()
