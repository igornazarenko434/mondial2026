"""Day-9.24 defensive: prove the per-person feature does NOT leak into the
other 5 message types. They must render IDENTICALLY whether
STRATEGY_OVERRIDES is set or not.

Locks in the design contract:
  • Only 🃏 match cards (render_card) display per-person suggestions.
  • 📊 standings sync, ☀️ daily summary, ⚽ kickoff card — all untouched.

Without this test, a future refactor could accidentally start reading
STRATEGY_OVERRIDES inside the other formatters and produce inconsistent
output across message types.
"""
from __future__ import annotations
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    with open("store/schema.sql") as f:
        c.executescript(f.read())
    return c


def _at(local_dt_str: str) -> datetime:
    return (datetime.strptime(local_dt_str, "%Y-%m-%d %H:%M")
                    .replace(tzinfo=ZoneInfo("Asia/Jerusalem"))
                    .astimezone(timezone.utc))


def _set_overrides(monkeypatch, value=None):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.setenv("FRIEND_PARTICIPANTS", "Vaadia")
    if value is None:
        monkeypatch.delenv("STRATEGY_OVERRIDES", raising=False)
    else:
        monkeypatch.setenv("STRATEGY_OVERRIDES", value)


# ───────────────── 📊 standings sync ─────────────────

def test_standings_summary_identical_with_or_without_overrides(monkeypatch):
    """Per-person env vars must have ZERO effect on the 📊 format."""
    from tools.sync_negev_standings import _format_telegram_summary
    rows = [
        {"player": "Igor", "rank": 1, "total": 10.0, "direction": 8,
         "broad": 2, "exactCount": 0, "role": "player"},
        {"player": "Vaadia", "rank": 2, "total": 5.0, "direction": 3,
         "broad": 2, "exactCount": 0, "role": "player"},
    ]

    _set_overrides(monkeypatch, None)
    title_a, body_a = _format_telegram_summary(rows, me="Igor", tid="t")

    _set_overrides(monkeypatch, '{"Vaadia": 0.4}')
    title_b, body_b = _format_telegram_summary(rows, me="Igor", tid="t")

    assert title_a == title_b
    assert body_a == body_b
    # Per-person UI must NEVER appear in the standings message
    assert "🎯" not in body_b
    assert "Per-person" not in body_b


# ───────────────── ☀️ daily summary ─────────────────

def test_daily_summary_identical_with_or_without_overrides(conn, monkeypatch):
    from schedule.daily_summary import build_summary_text
    # Seed: 1 today match + standings rows + Negev fake
    ko = _at("2026-06-11 22:00").isoformat()
    conn.execute(
        "INSERT INTO matches (match_id, utc_kickoff, stage, grp, home, away, status) "
        "VALUES (1, ?, 'Group', 'A', 'Mexico', 'South Africa', 'SCHEDULED')",
        (ko,))
    conn.execute(
        "INSERT INTO standings (participant, group_points, knockout_points, futures_points) "
        "VALUES ('Igor', 0, 0, 0)")
    conn.commit()

    from integrations import negev_toto_mcp as ntm
    fake_rows = [
        {"player": "Igor", "rank": 56, "total": 0.0, "direction": 0,
         "broad": 0, "role": "player"},
        {"player": "Vaadia", "rank": 12, "total": 3.5, "direction": 3.5,
         "broad": 0, "role": "player"},
    ]
    monkeypatch.setattr(ntm, "toto_get_standings", lambda **_: fake_rows)
    now = _at("2026-06-11 09:00")

    _set_overrides(monkeypatch, None)
    txt_a = build_summary_text(conn, now)

    _set_overrides(monkeypatch, '{"Vaadia": 0.4}')
    txt_b = build_summary_text(conn, now)

    assert txt_a == txt_b
    assert "🎯" not in txt_b
    assert "Per-person" not in txt_b


# ───────────────── ⚽ kickoff card ─────────────────

def test_kickoff_card_identical_with_or_without_overrides(monkeypatch):
    from schedule.kickoff_cards import build_kickoff_text
    match = {"match_id": 1, "utc_kickoff": "2026-06-11T19:00:00+00:00",
             "stage": "Group", "group": "A",
             "home": "Mexico", "away": "South Africa"}
    picks = [{"displayName": "Vaadia", "homeScore": 1, "awayScore": 1, "points": 0}]
    my_pred = {"home": 2, "away": 1}
    standings = [
        {"player": "Igor", "rank": 56, "total": 0.0,
         "direction": 0, "broad": 0, "role": "player"},
        {"player": "Vaadia", "rank": 12, "total": 3.5,
         "direction": 3.5, "broad": 0, "role": "player"},
    ]
    now = _at("2026-06-11 22:05")

    _set_overrides(monkeypatch, None)
    title_a, body_a = build_kickoff_text(match, now, picks, my_pred,
                                            standings, None)

    _set_overrides(monkeypatch, '{"Vaadia": 0.4}')
    title_b, body_b = build_kickoff_text(match, now, picks, my_pred,
                                            standings, None)

    assert title_a == title_b
    assert body_a == body_b
    assert "🎯" not in body_b
    assert "Per-person" not in body_b


# ───────────────── 🃏 match card — IS affected ─────────────────

def test_match_card_NO_section_when_overrides_unset(monkeypatch):
    """The render_card path MUST omit the per-person block when no overrides."""
    from core.delivery.base import render_card
    _set_overrides(monkeypatch, None)
    # Card with NO per_person_section — legacy shape
    card = {
        "home": "Mexico", "away": "South Africa", "stage": "Group", "group": "A",
        "kickoff_local": "now", "detonator": False,
        "locked_odds": {"H": 1.4, "D": 4.6, "A": 8.8},
        "model_prob":  {"H": .67, "D": .22, "A": .11},
        "pick_direction": "H", "pick_exact_score": {"home": 2, "away": 1},
        "modal_score": {"home": 2, "away": 1},
        "expected_points": 3.42,
        "signals_used": ["dixon_coles"], "signals_failed": [],
        "failure_reasons": {}, "ev_pathway": "ev",
    }
    body = render_card(card)
    assert "🎯" not in body
    assert "Per-person" not in body


def test_match_card_RENDERS_section_when_field_present(monkeypatch):
    """And renders it when build_card stamps the per_person_section field."""
    from core.delivery.base import render_card
    _set_overrides(monkeypatch, '{"Vaadia": 0.4}')
    card = {
        "home": "Mexico", "away": "South Africa", "stage": "Group", "group": "A",
        "kickoff_local": "now", "detonator": False,
        "locked_odds": {"H": 1.4, "D": 4.6, "A": 8.8},
        "model_prob":  {"H": .67, "D": .22, "A": .11},
        "pick_direction": "H", "pick_exact_score": {"home": 2, "away": 1},
        "modal_score": {"home": 2, "away": 1},
        "expected_points": 3.42,
        "signals_used": ["dixon_coles"], "signals_failed": [],
        "failure_reasons": {}, "ev_pathway": "ev",
        "per_person_section": ("🎯 Per-person suggestions\n"
                                 "  👤 Igor  (tilt 0.00, rank 56):  Mexico 2 — "
                                 "South Africa 1   EV 3.42"),
    }
    body = render_card(card)
    assert "🎯 Per-person suggestions" in body
    assert "Igor" in body
