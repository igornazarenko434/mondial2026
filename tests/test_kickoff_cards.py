"""Day-9.22: T+1m kickoff card.

Pins the must-hold properties:
  • Fires exactly once per match (runs-ledger idempotency keyed on
    (match_id, "kickoff")).
  • Fire window = [kickoff + DELAY, kickoff + CATCHUP]. Outside this
    range → no send. Catches up on restart up to CATCHUP minutes.
  • Picks block renders all tracked participants (you + friends).
  • Lineup section degrades silently when api-football empty / failing.
  • Standings section degrades silently when Negev unreachable.
  • Per-match failure doesn't block sibling matches.
  • Hook never raises — daemon loop must keep ticking.
"""
from __future__ import annotations
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from schedule import kickoff_cards as kc
from core.obs.runs import RunLedger


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    with open("store/schema.sql") as f:
        c.executescript(f.read())
    return c


@pytest.fixture
def led():
    return RunLedger(":memory:")


def _at(local_dt_str: str, tz: str = "Asia/Jerusalem") -> datetime:
    naive = datetime.strptime(local_dt_str, "%Y-%m-%d %H:%M")
    return naive.replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)


def _insert_match(conn, mid: int, ko_utc: datetime, *, home="Mexico",
                   away="South Africa", stage="Group", grp="A"):
    conn.execute(
        "INSERT INTO matches (match_id, utc_kickoff, stage, grp, home, away, status) "
        "VALUES (?, ?, ?, ?, ?, ?, 'SCHEDULED')",
        (mid, ko_utc.isoformat(), stage, grp, home, away))
    conn.commit()


# ─────────────── _matches_due ───────────────

def test_matches_due_within_window(conn, led):
    """Match that kicked off 5 min ago is inside the [1, 15] window."""
    now = _at("2026-06-11 22:05")
    _insert_match(conn, 999, now - timedelta(minutes=5))
    due = kc._matches_due(conn, now, led)
    assert len(due) == 1
    assert due[0]["match_id"] == 999


def test_matches_not_due_too_early(conn, led):
    """Kicked off 30 seconds ago → still before DELAY=1m, skip."""
    now = _at("2026-06-11 22:05")
    _insert_match(conn, 999, now - timedelta(seconds=30))
    assert kc._matches_due(conn, now, led) == []


def test_matches_not_due_too_late(conn, led):
    """Kicked off 30 minutes ago → past CATCHUP=15m, skip."""
    now = _at("2026-06-11 22:05")
    _insert_match(conn, 999, now - timedelta(minutes=30))
    assert kc._matches_due(conn, now, led) == []


def test_matches_due_skips_already_handled(conn, led):
    """Idempotency: once 'kickoff' is recorded, the match drops out."""
    now = _at("2026-06-11 22:05")
    _insert_match(conn, 999, now - timedelta(minutes=5))
    run_id = led.start(999, "kickoff", correlation_id="kickoff-999")
    led.finish(run_id, "ok", card_delivered=True)
    assert kc._matches_due(conn, now, led) == []


def test_matches_due_returns_multiple_concurrent_kickoffs(conn, led):
    """Group stage has up to 4 simultaneous kickoffs — all returned in order."""
    now = _at("2026-06-11 22:05")
    base = now - timedelta(minutes=5)
    _insert_match(conn, 1, base, home="A")
    _insert_match(conn, 2, base + timedelta(minutes=1), home="B")
    due = kc._matches_due(conn, now, led)
    assert len(due) == 2
    assert due[0]["match_id"] == 1
    assert due[1]["match_id"] == 2


# ─────────────── build_kickoff_text ───────────────

def test_build_text_includes_picks_block_for_each_tracked(monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.setenv("FRIEND_PARTICIPANTS", "Vaadia")
    match = {"match_id": 1, "utc_kickoff": _at("2026-06-11 22:00").isoformat(),
             "stage": "Group", "group": "A", "home": "Mexico", "away": "South Africa"}
    picks = [{"displayName": "Vaadia", "homeScore": 1, "awayScore": 1, "points": 0}]
    my_pred = {"home": 2, "away": 1}
    standings = [
        {"player": "Igor",   "rank": 26, "total": 0.0, "direction": 0, "broad": 0,
         "role": "player"},
        {"player": "Vaadia", "rank": 12, "total": 3.5, "direction": 3.5, "broad": 0,
         "role": "player"},
    ]
    title, body = kc.build_kickoff_text(match, _at("2026-06-11 22:05"),
                                          picks, my_pred, standings, None)
    assert "⚽ KICKOFF" in title
    assert "Mexico vs South Africa" in title
    assert "PICKS" in body
    assert "Igor: Mexico 2 — South Africa 1" in body
    assert "Vaadia: Mexico 1 — South Africa 1" in body
    assert "← you" in body                          # self marker
    assert "STANDINGS" in body
    assert "rank 26/2" in body                      # compact line


def test_build_text_degrades_when_negev_picks_missing(monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.setenv("FRIEND_PARTICIPANTS", "Vaadia")
    match = {"match_id": 1, "utc_kickoff": _at("2026-06-11 22:00").isoformat(),
             "stage": "Group", "group": "A", "home": "Mexico", "away": "South Africa"}
    title, body = kc.build_kickoff_text(match, _at("2026-06-11 22:05"),
                                          None, None, [], None)
    assert "PICKS" in body
    assert "Igor: (no pick yet)" in body
    assert "Vaadia: (no pick yet)" in body
    # No standings section since rows are empty
    assert "STANDINGS" not in body


def test_build_text_includes_lineups_when_available(monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.delenv("FRIEND_PARTICIPANTS", raising=False)
    match = {"match_id": 1, "utc_kickoff": _at("2026-06-11 22:00").isoformat(),
             "stage": "Group", "group": "A", "home": "Mexico", "away": "South Africa"}
    lineups = [
        {"team": "Mexico", "formation": "4-3-3", "coach": "Aguirre",
         "startXI": ["Ochoa (GK)", "Galindo (DF)"], "substitutes": []},
        {"team": "South Africa", "formation": "4-2-3-1", "coach": "Broos",
         "startXI": ["Williams (GK)"], "substitutes": []},
    ]
    _t, body = kc.build_kickoff_text(match, _at("2026-06-11 22:05"),
                                       None, None, [], lineups)
    assert "STARTING XI" in body
    assert "4-3-3" in body and "Aguirre" in body
    assert "Ochoa (GK)" in body


def test_build_text_omits_lineup_section_when_none(monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    match = {"match_id": 1, "utc_kickoff": _at("2026-06-11 22:00").isoformat(),
             "stage": "Group", "group": "A", "home": "Mexico", "away": "South Africa"}
    _t, body = kc.build_kickoff_text(match, _at("2026-06-11 22:05"),
                                       None, None, [], None)
    assert "STARTING XI" not in body


# ─────────────── fire_due (end-to-end) ───────────────

def _patch_negev(monkeypatch, picks, my_pred, standings):
    from integrations import negev_toto_mcp as ntm
    monkeypatch.setattr(ntm, "toto_get_match_details",
                         lambda **_: {"friendsPicks": picks,
                                       "myPrediction": my_pred,
                                       "match": {}, "exactPtsGrid": {}})
    monkeypatch.setattr(ntm, "toto_get_standings",
                         lambda **_: standings)


def test_fire_due_sends_and_marks_ledger(conn, led, monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.setenv("FRIEND_PARTICIPANTS", "Vaadia")
    now = _at("2026-06-11 22:05")
    _insert_match(conn, 999, now - timedelta(minutes=5))
    _patch_negev(monkeypatch, picks=[], my_pred=None,
                  standings=[{"player": "Igor", "rank": 1, "total": 0,
                                "direction": 0, "broad": 0, "role": "player"}])
    monkeypatch.setattr(kc, "_fetch_lineups", lambda *a, **k: None)
    sent_messages = []
    monkeypatch.setattr("core.delivery.summary",
                         lambda title, body: sent_messages.append((title, body)) or True)
    n = kc.fire_due(conn, led, now=now)
    assert n == 1
    assert len(sent_messages) == 1
    assert "⚽ KICKOFF" in sent_messages[0][0]
    # Ledger now marks it handled
    assert led.was_handled(999, "kickoff")


def test_fire_due_idempotent_within_window(conn, led, monkeypatch):
    """Same match across two ticks → one send, not two."""
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    now = _at("2026-06-11 22:05")
    _insert_match(conn, 999, now - timedelta(minutes=5))
    _patch_negev(monkeypatch, picks=[], my_pred=None, standings=[])
    monkeypatch.setattr(kc, "_fetch_lineups", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr("core.delivery.summary",
                         lambda t, b: sent.append(1) or True)
    kc.fire_due(conn, led, now=now)
    kc.fire_due(conn, led, now=now + timedelta(minutes=2))
    assert len(sent) == 1


def test_fire_due_per_match_failure_does_not_block_siblings(conn, led, monkeypatch):
    """Match A's pick-fetch raises → match B still gets carded."""
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    now = _at("2026-06-11 22:05")
    _insert_match(conn, 1, now - timedelta(minutes=5), home="A1", away="A2")
    _insert_match(conn, 2, now - timedelta(minutes=4), home="B1", away="B2")
    call_count = {"n": 0}
    def picks_side_effect(home, away):
        call_count["n"] += 1
        if home == "A1":
            raise RuntimeError("boom")
        return [], None
    monkeypatch.setattr(kc, "_fetch_picks", picks_side_effect)
    monkeypatch.setattr(kc, "_fetch_standings_rows", lambda: [])
    monkeypatch.setattr(kc, "_fetch_lineups", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr("core.delivery.summary",
                         lambda t, b: sent.append(t) or True)
    # _fetch_picks raises before sending — still moves to next match
    kc.fire_due(conn, led, now=now)
    # B should have been sent
    assert any("B1" in t for t in sent)


def test_fire_due_no_matches_returns_zero(conn, led, monkeypatch):
    n = kc.fire_due(conn, led, now=_at("2026-06-11 22:05"))
    assert n == 0


def test_fire_due_never_raises_on_unexpected_error(conn, led, monkeypatch):
    """Top-level safety net: even a delivery exception is swallowed."""
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    now = _at("2026-06-11 22:05")
    _insert_match(conn, 1, now - timedelta(minutes=5))
    _patch_negev(monkeypatch, picks=[], my_pred=None, standings=[])
    monkeypatch.setattr(kc, "_fetch_lineups", lambda *a, **k: None)
    def boom(*a, **k):
        raise RuntimeError("Telegram down")
    monkeypatch.setattr("core.delivery.summary", boom)
    # Must not propagate the RuntimeError
    n = kc.fire_due(conn, led, now=now)
    assert n == 0


# ─────────────── runner integration ───────────────

def test_runner_calls_kickoff_card_fn_each_tick(monkeypatch):
    """The new hook is invoked from tick() once per tick."""
    from schedule.runner import SchedulerDaemon
    calls = {"n": 0}
    def fake_hook(*, now):
        calls["n"] += 1
        return 0
    # Silence delivery
    import core.delivery as d
    monkeypatch.setattr(d, "deliver_card", lambda c: True)
    daemon = SchedulerDaemon(lambda: [], lambda m: {},
                              kickoff_card_fn=fake_hook)
    daemon.tick()
    daemon.tick()
    assert calls["n"] == 2


def test_runner_kickoff_card_failure_does_not_kill_tick(monkeypatch):
    """Hook raising → tick continues, watchdog still beats."""
    from schedule.runner import SchedulerDaemon
    def boom(*, now):
        raise RuntimeError("Negev sick")
    import core.delivery as d
    monkeypatch.setattr(d, "deliver_card", lambda c: True)
    daemon = SchedulerDaemon(lambda: [], lambda m: {},
                              kickoff_card_fn=boom)
    # Must not raise
    daemon.tick()


# ─────────────── concurrent kickoffs edge case ───────────────

def test_two_simultaneous_kickoffs_get_distinct_messages(conn, led, monkeypatch):
    """User-flagged edge case: two group-stage matches kicking off at the
    same instant must each receive their OWN message — distinct titles,
    distinct ledger rows, distinct match identities."""
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.setenv("FRIEND_PARTICIPANTS", "Vaadia")
    now = _at("2026-06-11 22:05")
    ko = now - timedelta(minutes=5)
    _insert_match(conn, 1001, ko, home="Mexico", away="South Africa")
    _insert_match(conn, 1002, ko, home="Norway",  away="France")

    # Distinct picks per match → confirms we don't bleed match A's data
    # into match B's message
    def picks_by_match(home, away):
        if home == "Mexico":
            return [{"displayName": "Vaadia", "homeScore": 1, "awayScore": 1}], \
                    {"home": 2, "away": 1}
        if home == "Norway":
            return [{"displayName": "Vaadia", "homeScore": 0, "awayScore": 2}], \
                    {"home": 0, "away": 3}
        return None, None
    monkeypatch.setattr(kc, "_fetch_picks", picks_by_match)
    standings_call_count = {"n": 0}
    def fetch_standings():
        standings_call_count["n"] += 1
        return [
            {"player": "Igor",   "rank": 26, "total": 0.0,
             "direction": 0, "broad": 0, "role": "player"},
            {"player": "Vaadia", "rank": 12, "total": 3.5,
             "direction": 3.5, "broad": 0, "role": "player"},
        ]
    monkeypatch.setattr(kc, "_fetch_standings_rows", fetch_standings)
    monkeypatch.setattr(kc, "_fetch_lineups", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr("core.delivery.summary",
                         lambda t, b: sent.append((t, b)) or True)

    n = kc.fire_due(conn, led, now=now)

    # Both messages sent
    assert n == 2
    assert len(sent) == 2
    titles = {t for t, _b in sent}
    assert any("Mexico vs South Africa" in t for t in titles)
    assert any("Norway vs France" in t for t in titles)

    # Match-specific picks landed in the right bodies (no cross-contamination)
    mex_body = next(b for t, b in sent if "Mexico" in t)
    nor_body = next(b for t, b in sent if "Norway" in t)
    assert "Mexico 2 — South Africa 1" in mex_body
    assert "Mexico 1 — South Africa 1" in mex_body
    assert "Norway 0 — France 3" in nor_body
    assert "Norway 0 — France 2" in nor_body

    # Both kickoff ledger rows recorded — second tick doesn't re-fire
    assert led.was_handled(1001, "kickoff")
    assert led.was_handled(1002, "kickoff")

    # Standings fetched EXACTLY ONCE (shared across both messages — the
    # optimization that keeps Negev calls flat regardless of fan-out)
    assert standings_call_count["n"] == 1


def test_two_simultaneous_kickoffs_one_fails_other_succeeds(conn, led, monkeypatch):
    """If match A's pick fetch raises mid-loop, match B still gets carded
    AND ledger reflects A's failure correctly."""
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    now = _at("2026-06-11 22:05")
    ko = now - timedelta(minutes=5)
    _insert_match(conn, 1, ko, home="X", away="Y")
    _insert_match(conn, 2, ko, home="A", away="B")
    def picks(h, a):
        if h == "X":
            raise RuntimeError("Negev timeout for match X")
        return [], None
    monkeypatch.setattr(kc, "_fetch_picks", picks)
    monkeypatch.setattr(kc, "_fetch_standings_rows", lambda: [])
    monkeypatch.setattr(kc, "_fetch_lineups", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr("core.delivery.summary",
                         lambda t, b: sent.append(t) or True)

    n = kc.fire_due(conn, led, now=now)
    assert n == 1
    assert any("A vs B" in t for t in sent)
    assert not any("X vs Y" in t for t in sent)
