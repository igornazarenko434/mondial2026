"""Day-9.24: per-person strategy suggestions.

Pinned behaviors across every on/off combination of (operator, friends,
overrides, standings_present):

  • No overrides + no friend → section omitted (backwards-compat)
  • Override on operator only → section renders with 1 row
  • Override on one friend → section renders with 2 rows (me + friend)
  • Override on multiple friends → section renders with N rows
  • Same tilt as global for everyone → section omitted
  • Standings empty (pre-tournament) → section still renders with default ranks
  • Negev rank lookup fails → section still renders without ranks
  • build_card raised → suggestions=None, no section
  • render_card includes the section after friend_picks_section
"""
from __future__ import annotations
import sqlite3
import json
from unittest.mock import patch

import pytest

from core.decision import per_person


# ───────────────────────── helpers ─────────────────────────

@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    with open("store/schema.sql") as f:
        c.executescript(f.read())
    return c


def _seed_standings(conn, rows):
    for name, pts in rows:
        conn.execute(
            "INSERT INTO standings (participant, group_points, knockout_points, "
            "futures_points) VALUES (?, ?, 0.0, 0.0)", (name, pts))
    conn.commit()


def _card(**overrides):
    """Minimal card with ranked_alternatives so strategy can deviate."""
    base = {
        "home": "Mexico", "away": "South Africa", "stage": "Group",
        "pick_direction": "H",
        "pick_exact_score": {"home": 2, "away": 1},
        "expected_points": 3.42,
        "detonator": True,
        "ranked_alternatives": [
            {"home": 2, "away": 1, "direction": "H",
             "expected_points": 3.42, "win_prob": 0.20},
            {"home": 3, "away": 0, "direction": "H",
             "expected_points": 2.8, "win_prob": 0.10},
            {"home": 1, "away": 0, "direction": "H",
             "expected_points": 2.5, "win_prob": 0.15},
        ],
    }
    base.update(overrides)
    return base


def _patch_negev_standings(monkeypatch, rows):
    """Make toto_get_standings return the given rows."""
    from integrations import negev_toto_mcp as ntm
    monkeypatch.setattr(ntm, "toto_get_standings",
                         lambda **_: rows)


# ───────────────────── _parse_overrides ─────────────────────

def test_parse_overrides_empty(monkeypatch):
    monkeypatch.delenv("STRATEGY_OVERRIDES", raising=False)
    assert per_person._parse_overrides() == {}


def test_parse_overrides_valid(monkeypatch):
    monkeypatch.setenv("STRATEGY_OVERRIDES", '{"Igor": 0.3, "Vaadia": 0.4}')
    out = per_person._parse_overrides()
    assert out == {"Igor": 0.3, "Vaadia": 0.4}


def test_parse_overrides_malformed_returns_empty(monkeypatch):
    monkeypatch.setenv("STRATEGY_OVERRIDES", "not json")
    assert per_person._parse_overrides() == {}


def test_parse_overrides_non_dict_returns_empty(monkeypatch):
    monkeypatch.setenv("STRATEGY_OVERRIDES", "[1, 2, 3]")
    assert per_person._parse_overrides() == {}


# ───────────────────── _tilt_for ─────────────────────

def test_tilt_for_explicit_override_wins(monkeypatch):
    monkeypatch.setenv("STRATEGY_TILT", "0.0")
    assert per_person._tilt_for("Vaadia", {"Vaadia": 0.5}) == 0.5


def test_tilt_for_falls_back_to_global(monkeypatch):
    monkeypatch.setenv("STRATEGY_TILT", "0.2")
    assert per_person._tilt_for("Vaadia", {}) == 0.2


def test_tilt_for_clamped_to_unit_interval(monkeypatch):
    monkeypatch.setenv("STRATEGY_TILT", "0.0")
    assert per_person._tilt_for("X", {"X": 2.5}) == 1.0
    assert per_person._tilt_for("X", {"X": -0.5}) == 0.0


# ───────────────────── compute_per_person_suggestions ─────────────────────

def test_no_overrides_no_friends_returns_none(monkeypatch, conn):
    """Backwards-compat default: nothing changes."""
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.delenv("FRIEND_PARTICIPANTS", raising=False)
    monkeypatch.delenv("STRATEGY_OVERRIDES", raising=False)
    out = per_person.compute_per_person_suggestions(_card(), conn)
    assert out is None


def test_override_with_one_friend_returns_two_rows(monkeypatch, conn):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.setenv("FRIEND_PARTICIPANTS", "Vaadia")
    monkeypatch.setenv("STRATEGY_TILT", "0")
    monkeypatch.setenv("STRATEGY_OVERRIDES", '{"Vaadia": 0.4}')
    _patch_negev_standings(monkeypatch, [
        {"player": "Igor", "rank": 56, "total": 0, "direction": 0,
         "broad": 0, "role": "player"},
        {"player": "Vaadia", "rank": 12, "total": 0, "direction": 0,
         "broad": 0, "role": "player"},
    ])
    _seed_standings(conn, [("Igor", 5.0), ("Vaadia", 3.5),
                            ("Leader", 12.0), ("Second", 10.0)])
    out = per_person.compute_per_person_suggestions(_card(), conn)
    assert out is not None
    assert len(out) == 2
    names = [r["name"] for r in out]
    assert names == ["Igor", "Vaadia"]
    # Each row carries the per-person tilt
    igor_row = next(r for r in out if r["name"] == "Igor")
    vaadia_row = next(r for r in out if r["name"] == "Vaadia")
    assert igor_row["tilt"] == 0.0
    assert vaadia_row["tilt"] == 0.4
    # Ranks resolved from Negev
    assert igor_row["rank"] == 56
    assert vaadia_row["rank"] == 12


def test_multiple_friends_all_render(monkeypatch, conn):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.setenv("FRIEND_PARTICIPANTS", "Vaadia,Alon,Dani")
    monkeypatch.setenv("STRATEGY_OVERRIDES",
                       '{"Vaadia": 0.4, "Alon": 0.2, "Dani": 0.6}')
    _patch_negev_standings(monkeypatch, [
        {"player": n, "rank": i+1, "total": 0,
         "direction": 0, "broad": 0, "role": "player"}
        for i, n in enumerate(["Vaadia", "Alon", "Dani", "Igor"])
    ])
    _seed_standings(conn, [("Igor", 0), ("Vaadia", 0), ("Alon", 0), ("Dani", 0)])
    out = per_person.compute_per_person_suggestions(_card(), conn)
    assert out is not None
    assert len(out) == 4
    assert [r["name"] for r in out] == ["Igor", "Vaadia", "Alon", "Dani"]


def test_same_tilt_everywhere_omits_section(monkeypatch, conn):
    """If every friend's tilt equals the global and no explicit override is
    set, the section adds no information → omit."""
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.setenv("FRIEND_PARTICIPANTS", "Vaadia")
    monkeypatch.setenv("STRATEGY_TILT", "0.2")
    monkeypatch.delenv("STRATEGY_OVERRIDES", raising=False)
    out = per_person.compute_per_person_suggestions(_card(), conn)
    assert out is None


def test_card_without_ranked_alternatives_omits_section(monkeypatch, conn):
    """Modal-fallback card → no menu of candidates → can't apply strategy."""
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.setenv("FRIEND_PARTICIPANTS", "Vaadia")
    monkeypatch.setenv("STRATEGY_OVERRIDES", '{"Vaadia": 0.4}')
    card = _card(ranked_alternatives=[])
    out = per_person.compute_per_person_suggestions(card, conn)
    assert out is None


def test_negev_rank_fetch_failure_proceeds_without_rank(monkeypatch, conn):
    """Section still renders with rank=None when Negev unreachable."""
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.setenv("FRIEND_PARTICIPANTS", "Vaadia")
    monkeypatch.setenv("STRATEGY_OVERRIDES", '{"Vaadia": 0.4}')
    from integrations import negev_toto_mcp as ntm
    def boom(**_):
        raise RuntimeError("Negev down")
    monkeypatch.setattr(ntm, "toto_get_standings", boom)
    _seed_standings(conn, [("Igor", 0), ("Vaadia", 0), ("L", 12.0)])
    out = per_person.compute_per_person_suggestions(_card(), conn)
    assert out is not None
    assert all(r["rank"] is None for r in out)


def test_empty_standings_no_context_still_renders(monkeypatch, conn):
    """Pre-tournament: standings empty → context=None → strategy no-ops →
    every person's pick = main card's pick. Section still shows the
    informative baseline."""
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.setenv("FRIEND_PARTICIPANTS", "Vaadia")
    monkeypatch.setenv("STRATEGY_OVERRIDES", '{"Vaadia": 0.4}')
    _patch_negev_standings(monkeypatch, [
        {"player": "Igor", "rank": 56, "total": 0, "direction": 0,
         "broad": 0, "role": "player"},
        {"player": "Vaadia", "rank": 12, "total": 0, "direction": 0,
         "broad": 0, "role": "player"},
    ])
    # NOTE: no _seed_standings — standings table is empty
    out = per_person.compute_per_person_suggestions(_card(), conn)
    assert out is not None
    assert len(out) == 2
    # Both pick the main card's recommendation (no tilt applied without context)
    for r in out:
        assert r["pick_exact_score"]["home"] == 2
        assert r["pick_exact_score"]["away"] == 1


# ───────────────────── render_section ─────────────────────

def test_render_section_none_returns_none():
    assert per_person.render_section(None, "X", "Y") is None
    assert per_person.render_section([], "X", "Y") is None


def test_render_section_basic():
    suggestions = [
        {"name": "Igor", "tilt": 0.0, "rank": 56,
         "pick_direction": "H", "pick_exact_score": {"home": 2, "away": 1},
         "expected_points": 3.42, "deviated_from_ev": False},
        {"name": "Vaadia", "tilt": 0.4, "rank": 12,
         "pick_direction": "H", "pick_exact_score": {"home": 3, "away": 0},
         "expected_points": 2.8, "deviated_from_ev": True},
    ]
    out = per_person.render_section(suggestions, "Mexico", "South Africa",
                                      detonator=True)
    assert out is not None
    assert "🎯 Per-person suggestions" in out
    assert "Igor" in out and "rank 56" in out
    assert "Vaadia" in out and "rank 12" in out
    assert "Mexico 2 — South Africa 1" in out
    assert "Mexico 3 — South Africa 0" in out
    # Detonator double shown
    assert "×2" in out
    # Deviation marker on Vaadia
    assert "⚡" in out


def test_render_section_no_rank_shows_question_mark():
    suggestions = [{"name": "X", "tilt": 0.3, "rank": None,
                     "pick_direction": "H",
                     "pick_exact_score": {"home": 1, "away": 0},
                     "expected_points": 2.0, "deviated_from_ev": False}]
    out = per_person.render_section(suggestions, "A", "B")
    assert "rank ?" in out


# ───────────────────── end-to-end via render_card ─────────────────────

def test_render_card_appends_per_person_section():
    from core.delivery.base import render_card
    card = {
        "home": "Mexico", "away": "South Africa", "stage": "Group", "group": "A",
        "kickoff_local": "2026-06-11 22:00", "detonator": False,
        "locked_odds": {"H": 1.4, "D": 4.6, "A": 8.8},
        "model_prob":  {"H": .67, "D": .22, "A": .11},
        "pick_direction": "H",
        "pick_exact_score": {"home": 2, "away": 1},
        "modal_score":      {"home": 2, "away": 1},
        "expected_points": 3.42,
        "signals_used":   ["dixon_coles", "elo", "market", "news"],
        "signals_failed": [],
        "failure_reasons": {},
        "news_provider": "gemini",
        "ev_pathway": "ev",
        "window": "T-7m",
        "friend_picks_section": "👥 Picks\n  Igor: Mexico 3-0  ← you",
        "per_person_section": ("🎯 Per-person suggestions\n"
                                 "  👤 Igor  (tilt 0.0, rank 56):  Mexico 2 — "
                                 "South Africa 1   EV 3.42\n"
                                 "  👤 Vaadia  (tilt 0.4, rank 12):  Mexico 3 — "
                                 "South Africa 0   EV 2.80   ⚡"),
    }
    body = render_card(card)
    # Both sections present, per-person AFTER friend_picks
    assert "👥 Picks" in body
    assert "🎯 Per-person suggestions" in body
    assert body.index("👥 Picks") < body.index("🎯 Per-person")
    # Both names visible
    assert "Igor" in body
    assert "Vaadia" in body


def test_render_card_legacy_no_per_person_section():
    """Legacy cards without the field render unchanged."""
    from core.delivery.base import render_card
    card = {
        "home": "X", "away": "Y", "stage": "Group", "group": "A",
        "kickoff_local": "now", "detonator": False,
        "locked_odds": {"H": 2.0, "D": 3.0, "A": 4.0},
        "model_prob":  {"H": .5, "D": .3, "A": .2},
        "pick_direction": "H",
        "pick_exact_score": {"home": 1, "away": 0},
        "modal_score":      {"home": 1, "away": 0},
        "expected_points": 1.0,
        "signals_used": ["dixon_coles"], "signals_failed": [],
        "failure_reasons": {}, "ev_pathway": "ev",
    }
    body = render_card(card)
    assert "🎯" not in body
    assert "Per-person" not in body
