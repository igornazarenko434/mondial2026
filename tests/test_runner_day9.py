"""Day-9: events_cache batching, auto-standings, daily-summary hook + idempotency.

All tests are offline by stubbing every external boundary (fixtures_fn,
build_card, events_cache_fn, etc.). The hooks are None by default so the
existing Day-6 tests are untouched.
"""
from __future__ import annotations
import sqlite3
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from schedule.runner import SchedulerDaemon, ODDS_WINDOWS


def _match(mid, mins_to_ko, stage="Group"):
    ko = datetime.now(timezone.utc) + timedelta(minutes=mins_to_ko)
    return {"match_id": mid, "utc_kickoff": ko.isoformat(),
            "home": f"H{mid}", "away": f"A{mid}", "stage": stage}


def _silence_delivery(monkeypatch):
    import core.delivery as d
    monkeypatch.setattr(d, "deliver_card", lambda c: True)
    monkeypatch.setattr(d, "alert", lambda t, b: True)


# ─────────────── events_cache batching ───────────────

def test_events_cache_fetched_once_per_tick_when_t7m_due(monkeypatch):
    """At a tick where any odds-window job is due, fetch_all_odds runs EXACTLY
    once and the result rides into every build_card call."""
    _silence_delivery(monkeypatch)
    fetch_calls = {"n": 0}
    payload = [{"id": "evt1"}, {"id": "evt2"}]
    def fake_fetcher():
        fetch_calls["n"] += 1
        return payload

    seen_caches = []
    def build(match):
        seen_caches.append(match.get("_events_cache"))
        return {"home": match["home"], "away": match["away"], "stage": "Group",
                "pick_exact_score": {"home": 1, "away": 0}, "pick_direction": "H",
                "expected_points": 1.0, "model_prob": {"H": .5, "D": .3, "A": .2},
                "locked_odds": {"H": 2.0, "D": 3.0, "A": 4.0}}

    # Use a match 7 minutes out → T-60m, T-15m, T-7m ALL due in the same tick
    # (each is within catch-up). That's 3 jobs per match × 2 matches = 6 jobs.
    matches = [_match(9101, 7), _match(9202, 7)]
    daemon = SchedulerDaemon(lambda: matches, build,
                              events_cache_fn=fake_fetcher, max_workers=4)
    daemon.tick()
    daemon.pool.shutdown(wait=True)
    assert fetch_calls["n"] == 1                    # ONE batch fetch — that's the win
    assert len(seen_caches) == 6                    # all due jobs got the cache
    assert all(c is payload for c in seen_caches)   # SAME object, not refetched


def test_events_cache_skipped_when_only_t24h_due(monkeypatch):
    """T-24h is a news/preview window — no odds pulled. fetch_all_odds must
    NOT be called to save the credit."""
    _silence_delivery(monkeypatch)
    fetch_calls = {"n": 0}
    def fake_fetcher():
        fetch_calls["n"] += 1
        return []

    def build(match):
        return {"home": match["home"], "away": match["away"], "stage": "Group",
                "pick_exact_score": {"home": 1, "away": 0}, "pick_direction": "H",
                "expected_points": 1.0, "model_prob": {"H": .5, "D": .3, "A": .2},
                "locked_odds": {"H": 2.0, "D": 3.0, "A": 4.0}}

    # 24h out → only T-24h window is due
    matches = [_match(9101, 24 * 60)]
    daemon = SchedulerDaemon(lambda: matches, build,
                              events_cache_fn=fake_fetcher)
    daemon.tick()
    daemon.pool.shutdown(wait=True)
    assert fetch_calls["n"] == 0                    # no odds-window job → no fetch


def test_events_cache_fetcher_failure_falls_back_to_per_match(monkeypatch):
    """If fetch_all_odds raises, the tick must still dispatch — build_card
    receives events_cache=None and pulls per-match (its existing behavior)."""
    _silence_delivery(monkeypatch)
    def boom():
        raise RuntimeError("the-odds-api 502")
    seen = []
    def build(match):
        seen.append(match.get("_events_cache"))
        return {"home": match["home"], "away": match["away"], "stage": "Group",
                "pick_exact_score": {"home": 1, "away": 0}, "pick_direction": "H",
                "expected_points": 1.0, "model_prob": {"H": .5, "D": .3, "A": .2},
                "locked_odds": {"H": 2.0, "D": 3.0, "A": 4.0}}
    matches = [_match(9301, 7)]                     # built before lambda fires
    daemon = SchedulerDaemon(lambda: matches, build, events_cache_fn=boom)
    daemon.tick()
    daemon.pool.shutdown(wait=True)
    # 3 jobs (T-60m/T-15m/T-7m all due at 7min out); every one gets None.
    assert len(seen) == 3 and all(c is None for c in seen)


def test_no_events_cache_fn_means_no_batching_at_all(monkeypatch):
    """Pre-existing call sites (events_cache_fn=None) must behave identically
    to Day-6: each match gets events_cache=None and pulls per-match."""
    _silence_delivery(monkeypatch)
    seen = []
    def build(match):
        seen.append(match.get("_events_cache"))
        return {"home": match["home"], "away": match["away"], "stage": "Group",
                "pick_exact_score": {"home": 1, "away": 0}, "pick_direction": "H",
                "expected_points": 1.0, "model_prob": {"H": .5, "D": .3, "A": .2},
                "locked_odds": {"H": 2.0, "D": 3.0, "A": 4.0}}
    matches = [_match(9401, 7)]                     # built before lambda fires
    daemon = SchedulerDaemon(lambda: matches, build)
    daemon.tick()
    daemon.pool.shutdown(wait=True)
    # 3 jobs at 7min out; backwards-compat behaviour: every events_cache is None.
    assert len(seen) == 3 and all(c is None for c in seen)


def test_odds_windows_constant_does_not_include_t24h():
    """Regression: T-24h MUST be excluded from ODDS_WINDOWS or we'd burn an
    extra odds_api credit 23 hours before each match."""
    assert "T-24h" not in ODDS_WINDOWS
    assert set(ODDS_WINDOWS) == {"T-60m", "T-15m", "T-7m"}


# ─────────────── auto-standings post-ingest ───────────────

def test_standings_update_called_each_tick(monkeypatch):
    """The standings_update_fn fires once per tick (cheap pure-SQL op)."""
    _silence_delivery(monkeypatch)
    calls = {"n": 0}
    def fake_updater():
        calls["n"] += 1
    daemon = SchedulerDaemon(lambda: [], lambda m: {},
                              standings_update_fn=fake_updater)
    daemon.tick(); daemon.tick(); daemon.tick()
    daemon.pool.shutdown(wait=True)
    assert calls["n"] == 3


def test_standings_update_failure_does_not_kill_tick(monkeypatch):
    """If update_standings raises (e.g. corrupted DB row), the tick must
    still dispatch jobs + beat the watchdog."""
    _silence_delivery(monkeypatch)
    def boom():
        raise RuntimeError("disk full")
    def build(match):
        return {"home": match["home"], "away": match["away"], "stage": "Group",
                "pick_exact_score": {"home": 1, "away": 0}, "pick_direction": "H",
                "expected_points": 1.0, "model_prob": {"H": .5, "D": .3, "A": .2},
                "locked_odds": {"H": 2.0, "D": 3.0, "A": 4.0}}
    matches = [_match(9501, 7)]                     # built before lambda fires
    daemon = SchedulerDaemon(lambda: matches, build, standings_update_fn=boom)
    submitted = daemon.tick()                       # must NOT raise
    daemon.pool.shutdown(wait=True)
    assert (9501, "T-7m") in submitted


def test_no_standings_fn_means_no_op(monkeypatch):
    """Backwards-compat: existing call sites without the hook still work."""
    _silence_delivery(monkeypatch)
    daemon = SchedulerDaemon(lambda: [], lambda m: {})
    daemon.tick()                                   # no exception
    daemon.pool.shutdown(wait=True)


# ─────────────── daily-summary hook + idempotency ───────────────

def test_daily_summary_hook_called_each_tick_with_now(monkeypatch):
    _silence_delivery(monkeypatch)
    seen_now = []
    def fake_summary(*, now):
        seen_now.append(now)
        return False                                # not actually due
    daemon = SchedulerDaemon(lambda: [], lambda m: {},
                              daily_summary_fn=fake_summary)
    fake = datetime(2026, 6, 11, 5, 0, tzinfo=timezone.utc)
    daemon.tick(now=fake)
    assert seen_now == [fake]


def test_daily_summary_failure_does_not_kill_tick(monkeypatch):
    _silence_delivery(monkeypatch)
    def boom(*, now):
        raise RuntimeError("Telegram down")
    matches = [_match(9601, 7)]                     # built before lambda fires
    daemon = SchedulerDaemon(lambda: matches, lambda m: {
        "home": "A", "away": "B", "stage": "Group", "pick_direction": "H",
        "pick_exact_score": {"home": 1, "away": 0}, "expected_points": 1.0,
        "model_prob": {"H": .5, "D": .3, "A": .2},
        "locked_odds": {"H": 2.0, "D": 3.0, "A": 4.0}},
                              daily_summary_fn=boom)
    submitted = daemon.tick()                       # must NOT raise
    daemon.pool.shutdown(wait=True)
    assert (9601, "T-7m") in submitted


# ─────────────── workers default = 6 ───────────────

def test_default_max_workers_is_six(monkeypatch):
    """Day-9 bump: 4 → 6 covers up to 4 simultaneous group-stage kickoffs
    with 2 spare threads for slow LLM/Brave calls."""
    monkeypatch.delenv("SCHED_MAX_WORKERS", raising=False)
    daemon = SchedulerDaemon(lambda: [], lambda m: {})
    assert daemon.max_workers == 6
