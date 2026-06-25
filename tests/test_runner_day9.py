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
    extra odds_api credit 23 hours before each match.

    Day-9.31: the constant is now env-driven, so this regression check
    must verify the DEFAULT (env unset) rather than the live value —
    which on production might be set to T-7m only for budget reasons."""
    assert "T-24h" not in ODDS_WINDOWS                  # live value: T-24h still forbidden
    import importlib, os, schedule.runner as rn
    saved = os.environ.get("ODDS_WINDOWS")
    try:
        os.environ.pop("ODDS_WINDOWS", None)
        importlib.reload(rn)
        assert set(rn.ODDS_WINDOWS) == {"T-60m", "T-15m", "T-7m"}, \
            "DEFAULT (unset env) must be the 3-window cycle without T-24h"
    finally:
        if saved is not None:
            os.environ["ODDS_WINDOWS"] = saved
        importlib.reload(rn)


# ─── Day-9.31: env-driven ODDS_WINDOWS — budget-throttle simulation ───
# These tests prove that setting ODDS_WINDOWS=T-7m in .env results in
# the daemon skipping the events_cache fetch at T-60m and T-15m — that's
# the budget-saving behavior we ship to survive the last days of June 2026
# when the odds_api credit balance won't fit the full 3-window schedule.

def test_odds_windows_env_parser_t7m_only():
    """Direct unit test on the env-loading mechanism.

    Re-import the module with ODDS_WINDOWS=T-7m → constant equals
    ('T-7m',). The runtime daemon reads this constant when deciding
    whether to call the events_cache fetcher (`_fetch_events_cache_if_needed`)
    so changing the constant changes the dispatch behavior."""
    import importlib, os, schedule.runner as rn
    saved = os.environ.get("ODDS_WINDOWS")
    try:
        os.environ["ODDS_WINDOWS"] = "T-7m"
        importlib.reload(rn)
        assert rn.ODDS_WINDOWS == ("T-7m",)
    finally:
        if saved is None:
            os.environ.pop("ODDS_WINDOWS", None)
        else:
            os.environ["ODDS_WINDOWS"] = saved
        importlib.reload(rn)                         # restore for other tests


def test_odds_windows_env_parser_default_when_unset():
    """When ODDS_WINDOWS is not in the environment, default to the three-
    window cycle. This is the post-July-1-reset normal state."""
    import importlib, os, schedule.runner as rn
    saved = os.environ.pop("ODDS_WINDOWS", None)
    try:
        importlib.reload(rn)
        assert rn.ODDS_WINDOWS == ("T-60m", "T-15m", "T-7m")
    finally:
        if saved is not None:
            os.environ["ODDS_WINDOWS"] = saved
        importlib.reload(rn)


def test_odds_windows_env_empty_string_falls_back_to_default():
    """Safety: an accidentally-empty env value mustn't yield an empty tuple
    (which would silently disable ALL odds fetching). Falls back to default."""
    import importlib, os, schedule.runner as rn
    saved = os.environ.get("ODDS_WINDOWS")
    try:
        os.environ["ODDS_WINDOWS"] = ""
        importlib.reload(rn)
        assert rn.ODDS_WINDOWS == ("T-60m", "T-15m", "T-7m")
    finally:
        if saved is None:
            os.environ.pop("ODDS_WINDOWS", None)
        else:
            os.environ["ODDS_WINDOWS"] = saved
        importlib.reload(rn)


def test_t7m_only_skips_events_cache_at_t60m_dispatch(monkeypatch):
    """SIMULATION: with ODDS_WINDOWS=('T-7m',), dispatch a job whose ONLY
    due window is T-60m → events_cache_fn must NOT be called → ZERO
    odds_api credits charged for this tick.

    Without the fix (ODDS_WINDOWS contains T-60m), the same tick would
    burn 2 credits."""
    _silence_delivery(monkeypatch)
    import schedule.runner as rn
    monkeypatch.setattr(rn, "ODDS_WINDOWS", ("T-7m",))

    fetch_calls = {"n": 0}
    def fake_fetcher():
        fetch_calls["n"] += 1
        return []

    def build(match):
        return {"home": match["home"], "away": match["away"], "stage": "Group",
                "pick_exact_score": {"home": 1, "away": 0}, "pick_direction": "H",
                "expected_points": 1.0, "model_prob": {"H": .5, "D": .3, "A": .2},
                "locked_odds": {"H": 2.0, "D": 3.0, "A": 4.0}}

    # Match 60 minutes out → only T-60m window is due. With T-7m-only
    # config, this window is no longer an "odds window" → no fetch.
    matches = [_match(9601, 60)]
    daemon = rn.SchedulerDaemon(lambda: matches, build,
                                  events_cache_fn=fake_fetcher)
    daemon.tick()
    daemon.pool.shutdown(wait=True)
    assert fetch_calls["n"] == 0, "T-60m must not trigger odds fetch when only T-7m configured"


def test_t7m_only_still_fetches_at_t7m_dispatch(monkeypatch):
    """Conversely: with ODDS_WINDOWS=('T-7m',), a T-7m-due job MUST still
    trigger the events_cache fetch — the scoring lock window stays armed."""
    _silence_delivery(monkeypatch)
    import schedule.runner as rn
    monkeypatch.setattr(rn, "ODDS_WINDOWS", ("T-7m",))

    fetch_calls = {"n": 0}
    payload = [{"id": "evt"}]
    def fake_fetcher():
        fetch_calls["n"] += 1
        return payload

    seen = []
    def build(match):
        seen.append(match.get("_events_cache"))
        return {"home": match["home"], "away": match["away"], "stage": "Group",
                "pick_exact_score": {"home": 1, "away": 0}, "pick_direction": "H",
                "expected_points": 1.0, "model_prob": {"H": .5, "D": .3, "A": .2},
                "locked_odds": {"H": 2.0, "D": 3.0, "A": 4.0}}

    # Match 7 minutes out → T-60m/T-15m/T-7m all "due" but only T-7m is
    # an odds window now. Fetch happens because at least one due job's
    # window is in ODDS_WINDOWS.
    matches = [_match(9701, 7)]
    daemon = rn.SchedulerDaemon(lambda: matches, build,
                                  events_cache_fn=fake_fetcher)
    daemon.tick()
    daemon.pool.shutdown(wait=True)
    assert fetch_calls["n"] == 1, "T-7m must still fetch the scoring-lock odds"
    # All 3 dispatched jobs share the same cache (events_cache_fn called once)
    assert payload in seen


def test_skip_market_flag_stamped_when_window_not_in_odds_windows(monkeypatch):
    """REGRESSION-CLOSER: without this flag, build_card would receive
    events_cache=None at T-60m and fetch_match_odds would fall back to
    fetch_all_odds() per-match — burning credits we thought we were saving.
    Runner now stamps _skip_market=True so build_card can short-circuit
    odds_fetcher entirely."""
    _silence_delivery(monkeypatch)
    import schedule.runner as rn
    monkeypatch.setattr(rn, "ODDS_WINDOWS", ("T-7m",))

    seen_flags = []
    def build(match):
        seen_flags.append((match.get("_window"), match.get("_skip_market")))
        return {"home": match["home"], "away": match["away"], "stage": "Group",
                "pick_exact_score": {"home": 1, "away": 0}, "pick_direction": "H",
                "expected_points": 1.0, "model_prob": {"H": .5, "D": .3, "A": .2},
                "locked_odds": {"H": 2.0, "D": 3.0, "A": 4.0}}

    # 7 minutes out → all 3 windows (T-60m/T-15m/T-7m) due in same tick
    matches = [_match(9901, 7)]
    daemon = rn.SchedulerDaemon(lambda: matches, build,
                                  events_cache_fn=lambda: [], max_workers=4)
    daemon.tick()
    daemon.pool.shutdown(wait=True)
    # T-60m and T-15m get _skip_market=True (saves per-match fetch_all_odds);
    # T-7m gets _skip_market=False (scoring lock fires).
    by_window = dict(seen_flags)
    assert by_window == {"T-60m": True, "T-15m": True, "T-7m": False}


def test_skip_market_flag_default_false_when_all_windows_configured(monkeypatch):
    """Default config (no env override) → no window is "skip" → backwards-compat
    with all existing Day-9 events_cache batching behavior."""
    _silence_delivery(monkeypatch)
    import schedule.runner as rn
    monkeypatch.setattr(rn, "ODDS_WINDOWS", ("T-60m", "T-15m", "T-7m"))

    seen_flags = []
    def build(match):
        seen_flags.append((match.get("_window"), match.get("_skip_market")))
        return {"home": match["home"], "away": match["away"], "stage": "Group",
                "pick_exact_score": {"home": 1, "away": 0}, "pick_direction": "H",
                "expected_points": 1.0, "model_prob": {"H": .5, "D": .3, "A": .2},
                "locked_odds": {"H": 2.0, "D": 3.0, "A": 4.0}}

    matches = [_match(9902, 7)]
    daemon = rn.SchedulerDaemon(lambda: matches, build,
                                  events_cache_fn=lambda: [], max_workers=4)
    daemon.tick()
    daemon.pool.shutdown(wait=True)
    # All 3 windows get _skip_market=False (their natural behavior)
    by_window = dict(seen_flags)
    assert by_window == {"T-60m": False, "T-15m": False, "T-7m": False}


def test_credit_savings_simulation_full_day_jun27(monkeypatch):
    """Concrete budget simulation: Jun 27 has 4 distinct kickoff clusters
    (00:00, 03:00, 21:00, 23:30 IDT). With the default 3-window config,
    each cluster fires 3 fetches × 2 credits = 24 credits/day. With
    ODDS_WINDOWS='T-7m', each cluster fires 1 fetch × 2 credits = 8.

    This test simulates ONE cluster of that day; multiply by 4 mentally for
    the day total. Asserting once-per-cluster avoids a flaky 12-tick test."""
    _silence_delivery(monkeypatch)
    import schedule.runner as rn
    monkeypatch.setattr(rn, "ODDS_WINDOWS", ("T-7m",))

    fetch_calls = {"n": 0}
    def fake_fetcher():
        fetch_calls["n"] += 1
        return []

    def build(match):
        return {"home": match["home"], "away": match["away"], "stage": "Group",
                "pick_exact_score": {"home": 1, "away": 0}, "pick_direction": "H",
                "expected_points": 1.0, "model_prob": {"H": .5, "D": .3, "A": .2},
                "locked_odds": {"H": 2.0, "D": 3.0, "A": 4.0}}

    # Two matches in same kickoff cluster (both 7 min out) → events_cache
    # is fetched ONCE, both matches share the result. Without the fix this
    # would be 6 fetches (3 windows × 2 matches in same cluster — well,
    # 3 fetches because events_cache batches per tick). With the fix: 1.
    matches = [_match(9801, 7), _match(9802, 7)]
    daemon = rn.SchedulerDaemon(lambda: matches, build,
                                  events_cache_fn=fake_fetcher, max_workers=4)
    daemon.tick()
    daemon.pool.shutdown(wait=True)
    assert fetch_calls["n"] == 1, (
        "ONE fetch per cluster — 2 matches share, only T-7m window in pool"
    )


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
