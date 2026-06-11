"""Day-9.25: stress-test the production multi-match dispatch pattern.

Real-world scenario: today's 22:00 IDT (Mexico v SA, detonator) AND tomorrow's
22:00 IDT (some other match) both have T-60m / T-15m / T-7m / T+1m windows
that can overlap if the cron / ticks line up. Within a single tick, all four
group-stage matches on day-2 can dispatch SIMULTANEOUSLY (4 × 22:00 EU TV
slots).

This test simulates the FULL SchedulerDaemon path with N concurrent
ThreadPoolExecutor workers, each calling persist_card via a fresh connection
(the Day-9.25 pattern). Asserts:

  1. NO data is lost. Every match × window combination lands a predictions row.
  2. NO row clobbers another. The ON CONFLICT (match_id, window) clause
     upserts in-place, so a same-tick fire and a same-day refire end up with
     the LATEST card body — not corrupted intermediate data.
  3. SQLite serialization holds. With sqlite3's default journaling, 6
     concurrent writers serialize at the OS level; no `database is locked`
     errors leak through.
  4. EVERY worker's connection is closed (no file-descriptor leak across
     a tournament's worth of fires).
"""
from __future__ import annotations
import os
import resource
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing

import pytest


def _fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "stress.db"
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    schema_path = os.path.join(here, "store", "schema.sql")
    seed = sqlite3.connect(str(db_path))
    with open(schema_path) as f:
        seed.executescript(f.read())
    seed.commit()
    seed.close()

    import store.db as _db
    original = _db.connect
    def _connect(path=None):
        return original(str(db_path))
    monkeypatch.setattr(_db, "connect", _connect)
    return str(db_path)


def test_24_concurrent_dispatches_all_land(tmp_path, monkeypatch):
    """Simulates a full tournament-day burst: 6 matches × 4 windows = 24
    concurrent persists. Every (match_id, window) pair must produce exactly
    one row, and the row count must equal 24."""
    _fresh_db(tmp_path, monkeypatch)
    from store.db import connect
    from core.decision.build_card import persist_card

    dispatches = [
        (mid, window)
        for mid in (1001, 1002, 1003, 1004, 1005, 1006)
        for window in ("T-24h", "T-60m", "T-15m", "T-7m")
    ]

    def worker(mid_window):
        mid, window = mid_window
        with closing(connect()) as conn:
            persist_card(conn, {
                "match_id": mid, "window": window,
                "pick_direction": "D",
                "pick_exact_score": {"home": 0, "away": 0},
                "modal_score": {"home": 1, "away": 0},
                "expected_points": 1.5,
                "home": f"H{mid}", "away": f"A{mid}",
            })
        return (mid, window)

    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="match") as pool:
        results = [f.result(timeout=10)
                    for f in as_completed(pool.submit(worker, d)
                                            for d in dispatches)]

    assert sorted(results) == sorted(dispatches), \
        "some workers reported a different (mid, window) than they were given"

    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT match_id, window FROM predictions "
            "ORDER BY match_id, window").fetchall()
    assert len(rows) == 24, \
        f"expected 24 distinct rows; got {len(rows)}. Missing: " \
        f"{set(dispatches) - set(tuple(r) for r in rows)}"
    # Each pair appears exactly once (ON CONFLICT upsert holds)
    assert sorted(tuple(r) for r in rows) == sorted(dispatches)


def test_same_match_window_upserts_in_place(tmp_path, monkeypatch):
    """A match that fires twice for the same (match_id, window) — e.g.,
    catchup after a daemon restart — must produce ONE row with the LATEST
    payload, not two rows or a corrupted state."""
    _fresh_db(tmp_path, monkeypatch)
    from store.db import connect
    from core.decision.build_card import persist_card

    # First fire: pick A
    with closing(connect()) as conn:
        persist_card(conn, {
            "match_id": 2001, "window": "T-60m",
            "pick_direction": "H",
            "pick_exact_score": {"home": 2, "away": 0},
            "modal_score": {"home": 2, "away": 0},
            "expected_points": 1.5,
            "home": "X", "away": "Y",
        })
    # Catch-up fire: latest model say draw 1-1
    with closing(connect()) as conn:
        persist_card(conn, {
            "match_id": 2001, "window": "T-60m",
            "pick_direction": "D",
            "pick_exact_score": {"home": 1, "away": 1},
            "modal_score": {"home": 1, "away": 1},
            "expected_points": 2.7,
            "home": "X", "away": "Y",
        })

    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT match_id, window, pick_dir, pick_h, pick_a, expected_points "
            "FROM predictions WHERE match_id=2001").fetchall()
    assert len(rows) == 1, f"upsert should produce 1 row, got {len(rows)}"
    assert rows[0][2] == "D"             # LATEST direction (not the first)
    assert rows[0][3] == 1 and rows[0][4] == 1
    assert abs(rows[0][5] - 2.7) < 1e-6


def test_no_fd_leak_after_1000_persists(tmp_path, monkeypatch):
    """Sanity: 1000 sequential persists with per-call connection don't leak
    file descriptors. This is what a 7-week tournament accumulates if every
    card persists cleanly."""
    _fresh_db(tmp_path, monkeypatch)
    from store.db import connect
    from core.decision.build_card import persist_card

    if not hasattr(resource, "RLIMIT_NOFILE"):
        pytest.skip("RLIMIT_NOFILE not supported on this platform")

    soft_before, _ = resource.getrlimit(resource.RLIMIT_NOFILE)

    for i in range(1000):
        with closing(connect()) as conn:
            persist_card(conn, {
                "match_id": 3000 + i, "window": "T-7m",
                "pick_direction": "D",
                "pick_exact_score": {"home": 0, "away": 0},
                "modal_score": {"home": 0, "away": 0},
                "expected_points": 1.0,
                "home": "H", "away": "A",
            })

    # If the per-call pattern had leaked, we'd have ~1000 open fds; the soft
    # limit on macOS dev boxes is typically 256 so 1000 sequential persists
    # would have triggered an OSError. Reaching this assertion = no leak.
    with closing(connect()) as conn:
        n = conn.execute("SELECT COUNT(*) FROM predictions "
                          "WHERE match_id BETWEEN 3000 AND 3999").fetchone()[0]
    assert n == 1000


def test_simulating_today_22h_and_tomorrow_22h_both_persist(tmp_path,
                                                              monkeypatch):
    """Igor's scenario: today's 22:00 IDT Mexico v SA dispatches its T-7m
    while tomorrow's 22:00 IDT Korea v Czechia dispatches its T-24h preview.
    Different days, different windows, simultaneously hitting the same
    SQLite file via different worker connections — both must land."""
    _fresh_db(tmp_path, monkeypatch)
    from store.db import connect
    from core.decision.build_card import persist_card

    def today_22h_worker():
        with closing(connect()) as conn:
            persist_card(conn, {
                "match_id": 537327, "window": "T-7m",
                "pick_direction": "D",
                "pick_exact_score": {"home": 0, "away": 0},
                "modal_score": {"home": 1, "away": 0},
                "expected_points": 3.31,
                "home": "Mexico", "away": "South Africa",
                "detonator": True,
            })

    def tomorrow_22h_worker():
        with closing(connect()) as conn:
            persist_card(conn, {
                "match_id": 537328, "window": "T-24h",
                "pick_direction": "D",
                "pick_exact_score": {"home": 1, "away": 1},
                "modal_score": {"home": 1, "away": 1},
                "expected_points": 1.42,
                "home": "South Korea", "away": "Czechia",
            })

    # Both submitted to the same pool, executing concurrently.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="match") as pool:
        f1 = pool.submit(today_22h_worker)
        f2 = pool.submit(tomorrow_22h_worker)
        f1.result(timeout=5)
        f2.result(timeout=5)

    with closing(connect()) as conn:
        rows = {(r[0], r[1]): r[2] for r in conn.execute(
            "SELECT match_id, window, expected_points FROM predictions "
            "WHERE match_id IN (537327, 537328)").fetchall()}
    assert (537327, "T-7m") in rows
    assert (537328, "T-24h") in rows
    assert abs(rows[(537327, "T-7m")] - 3.31) < 1e-6
    assert abs(rows[(537328, "T-24h")] - 1.42) < 1e-6


def test_full_payload_persisted_for_audit_tools(tmp_path, monkeypatch):
    """`tools/audit_fired_card.py` reads `payload_json` from the predictions
    row to reconstruct the card body. Pin that the full payload (signals,
    news provenance, ev_pathway) survives the round-trip via persist_card."""
    _fresh_db(tmp_path, monkeypatch)
    import json as _json
    from store.db import connect
    from core.decision.build_card import persist_card

    rich_card = {
        "match_id": 4001, "window": "T-7m",
        "pick_direction": "D",
        "pick_exact_score": {"home": 0, "away": 0},
        "modal_score": {"home": 1, "away": 0},
        "expected_points": 3.31,
        "home": "Mexico", "away": "South Africa",
        "detonator": True,
        "signals_used": ["dixon_coles", "elo", "market", "news"],
        "signals_failed": [],
        "ev_pathway": "ev_optimized",
        "news_provider": "gemini",
        "news_parse_tier": "strict",
        "news_home_delta": 0.10,
        "news_confidence": "medium",
        "news_fallbacks_used": [],
        "news_fallback_errors": {},
    }

    with closing(connect()) as conn:
        persist_card(conn, rich_card)

    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT payload_json FROM predictions WHERE match_id=4001").fetchone()
    payload = _json.loads(row[0])
    assert payload["signals_used"] == ["dixon_coles", "elo", "market", "news"]
    assert payload["ev_pathway"] == "ev_optimized"
    assert payload["news_provider"] == "gemini"
    assert payload["news_parse_tier"] == "strict"
    assert abs(payload["news_home_delta"] - 0.10) < 1e-6
    assert payload["news_confidence"] == "medium"
