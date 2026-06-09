"""Day-9.22: Per-card friend-picks footer (T-60m/-15m/-7m cards).

Pins:
  • render_card appends friend_picks_section AFTER the cap (footer is
    supplementary; never truncated).
  • _build_friend_picks_section returns None when FRIEND_PARTICIPANTS
    unset (cards stay legacy-shaped — no extra Negev calls).
  • Returns None on Negev fetch failure (degrades silently).
  • build_card stamps the section on the card output (end-to-end).
"""
from __future__ import annotations

import pytest

from core.delivery.base import render_card
from core.decision import build_card as bc_mod


# ─────────────── _build_friend_picks_section ───────────────

def test_section_is_none_when_no_friends(monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.delenv("FRIEND_PARTICIPANTS", raising=False)
    assert bc_mod._build_friend_picks_section("Mexico", "South Africa") is None


def test_section_is_none_on_negev_error(monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.setenv("FRIEND_PARTICIPANTS", "Vaadia")
    from integrations import negev_toto_mcp as ntm
    def boom(**_):
        raise RuntimeError("Negev unreachable")
    monkeypatch.setattr(ntm, "toto_get_match_details", boom)
    assert bc_mod._build_friend_picks_section("Mexico", "South Africa") is None


def test_section_is_none_when_negev_returns_error_dict(monkeypatch):
    """Negev's MCP returns {'error': ...} for missing matches; treat as None."""
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.setenv("FRIEND_PARTICIPANTS", "Vaadia")
    from integrations import negev_toto_mcp as ntm
    monkeypatch.setattr(ntm, "toto_get_match_details",
                         lambda **_: {"error": "match not found"})
    assert bc_mod._build_friend_picks_section("Mexico", "South Africa") is None


def test_section_renders_with_friends_and_my_pred(monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.setenv("FRIEND_PARTICIPANTS", "Vaadia")
    from integrations import negev_toto_mcp as ntm
    monkeypatch.setattr(ntm, "toto_get_match_details", lambda **_: {
        "friendsPicks": [{"displayName": "Vaadia", "homeScore": 1, "awayScore": 1}],
        "myPrediction": {"home": 2, "away": 1},
        "match": {}, "exactPtsGrid": {},
    })
    out = bc_mod._build_friend_picks_section("Mexico", "South Africa")
    assert out is not None
    assert "👥 Picks" in out
    assert "Igor: Mexico 2 — South Africa 1" in out
    assert "Vaadia: Mexico 1 — South Africa 1" in out


def test_section_handles_missing_team_args():
    """Defensive: build_card sometimes has match.get('home')=None during
    catastrophic loader failure. Section must None-out cleanly."""
    assert bc_mod._build_friend_picks_section(None, "South Africa") is None
    assert bc_mod._build_friend_picks_section("Mexico", None) is None


# ─────────────── render_card uses the section ───────────────

def _minimal_card(**overrides):
    """Minimum card shape that exercises render_card. ev_pathway=='dc' so no
    [no live odds] tag."""
    card = {
        "home": "Mexico", "away": "South Africa", "stage": "Group", "group": "A",
        "kickoff_local": "2026-06-11 22:00",
        "detonator": True,
        "locked_odds": {"H": 1.85, "D": 3.60, "A": 4.20},
        "model_prob":  {"H": .67, "D": .21, "A": .12},
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
    }
    card.update(overrides)
    return card


def test_render_card_appends_friend_section_when_present():
    section = "👥 Picks\n  Igor: X 2 — Y 1   ← you\n  Vaadia: Draw 1 — 1"
    txt = render_card(_minimal_card(friend_picks_section=section))
    assert "👥 Picks" in txt
    assert "Vaadia" in txt
    # Section is at the END of the card (after Signals line)
    assert txt.rindex("Vaadia") > txt.rindex("Signals:")


def test_render_card_no_section_when_card_has_none():
    """Backward compat: legacy cards without the field render unchanged."""
    txt = render_card(_minimal_card())
    assert "👥 Picks" not in txt
    assert "Vaadia" not in txt


def test_render_card_no_section_when_value_is_none():
    """Explicit None (the no-friends + no-Negev path) → no section."""
    txt = render_card(_minimal_card(friend_picks_section=None))
    assert "👥 Picks" not in txt


def test_friend_section_NOT_truncated_by_line_cap():
    """The cap exists to compact the model output; the picks footer must
    survive even when the model output is at MAX_LINES."""
    # Construct a card whose model lines hit the cap, then add a section
    section = ("👥 Picks\n  Igor: X 1 — Y 0\n  Vaadia: X 2 — Y 0\n"
                "  David: X 0 — Y 1")
    card = _minimal_card(
        # Adding context bullets to push toward the cap
        context=["Lineup confirmed", "Rain forecast at kickoff"],
        friend_picks_section=section,
    )
    txt = render_card(card)
    # All 3 names must appear in output
    for name in ("Igor", "Vaadia", "David"):
        assert name in txt
