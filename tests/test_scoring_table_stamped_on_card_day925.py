"""Day-9.25: pin that build_card stamps the scoring-table choice on every card.

Why: the EV pick for a Group game and the SAME pick for a KO game would have
different expected_points because the exact_multiplier tables differ:
  • Group 1-0 → mult 1.5
  • KO 1-0    → mult 2.25
  • Final 1-0 → mult 3.0
If football_data ever publishes a stage code we forgot to map in
RULES_STAGE, STAGE_TYPE returns None → fall-through behavior could silently
use the wrong table. Stamping the chosen table + the actual multiplier
makes this auditable per-card via `tools/audit_fired_card.py`.
"""
from __future__ import annotations
import pytest

from core.decision.build_card import build_card


def _dummy_match(stage="Group"):
    return {
        "match_id": 99001, "home": "Mexico", "away": "South Africa",
        "stage": stage, "utc_kickoff": "2026-06-11T19:00:00+00:00",
        "group": "A", "detonator": False,
    }


def test_group_stage_card_stamps_scoring_table_group():
    card = build_card(_dummy_match(stage="Group"),
                      strengths_loader=lambda: {"teams": {}, "alpha": 0.0},
                      elo_loader=lambda: {"Mexico": 1700, "South Africa": 1500},
                      odds_fetcher=lambda *a, **k: {"H": 1.5, "D": 4.0, "A": 6.0},
                      news_analyzer=lambda *a, **k: {
                          "home_goal_delta": 0.0, "away_goal_delta": 0.0,
                          "confidence": "low", "notes": [], "provider": None})
    assert card.get("scoring_table") == "group", \
        f"Group match should stamp scoring_table='group', got {card.get('scoring_table')!r}"
    assert card.get("exact_multiplier_used") is not None
    # For a 0-0 group pick, the multiplier should be 2.75
    pe = card["pick_exact_score"]
    if pe == {"home": 0, "away": 0}:
        assert abs(card["exact_multiplier_used"] - 2.75) < 1e-6


def test_r16_card_stamps_scoring_table_ko():
    card = build_card(_dummy_match(stage="R16"),
                      strengths_loader=lambda: {"teams": {}, "alpha": 0.0},
                      elo_loader=lambda: {"Mexico": 1700, "South Africa": 1500},
                      odds_fetcher=lambda *a, **k: {"H": 1.5, "D": 4.0, "A": 6.0},
                      news_analyzer=lambda *a, **k: {
                          "home_goal_delta": 0.0, "away_goal_delta": 0.0,
                          "confidence": "low", "notes": [], "provider": None})
    assert card.get("scoring_table") == "ko"


def test_final_stage_card_stamps_scoring_table_final():
    card = build_card(_dummy_match(stage="Final"),
                      strengths_loader=lambda: {"teams": {}, "alpha": 0.0},
                      elo_loader=lambda: {"Mexico": 1700, "South Africa": 1500},
                      odds_fetcher=lambda *a, **k: {"H": 1.5, "D": 4.0, "A": 6.0},
                      news_analyzer=lambda *a, **k: {
                          "home_goal_delta": 0.0, "away_goal_delta": 0.0,
                          "confidence": "low", "notes": [], "provider": None})
    assert card.get("scoring_table") == "final"


def test_unknown_stage_does_not_crash_card_stamps_none():
    """Defense in depth: if football_data ever emits a stage we forgot to
    map, the stamp falls to None — but the card MUST still be produced.
    Audit_fired_card sees scoring_table=None and shows ⚠ DRIFT."""
    card = build_card(_dummy_match(stage="UnknownStage"),
                      strengths_loader=lambda: {"teams": {}, "alpha": 0.0},
                      elo_loader=lambda: {"Mexico": 1700, "South Africa": 1500},
                      odds_fetcher=lambda *a, **k: {"H": 1.5, "D": 4.0, "A": 6.0},
                      news_analyzer=lambda *a, **k: {
                          "home_goal_delta": 0.0, "away_goal_delta": 0.0,
                          "confidence": "low", "notes": [], "provider": None})
    # The card was produced (build_card NEVER raises per golden rule #10)
    assert card.get("home") == "Mexico"
    # And the stamp is None — caller can flag the drift
    assert card.get("scoring_table") is None


def test_stamped_multiplier_matches_engine_exact_multiplier():
    """The value stamped on the card must equal what exact_multiplier()
    returns — proves the chain (card.stage → STAGE_TYPE → SCORE_TABLE) is
    consistent end-to-end."""
    from config.rules import STAGE_TYPE
    from core.scoring.engine import exact_multiplier
    card = build_card(_dummy_match(stage="QF"),
                      strengths_loader=lambda: {"teams": {}, "alpha": 0.0},
                      elo_loader=lambda: {"Mexico": 1700, "South Africa": 1500},
                      odds_fetcher=lambda *a, **k: {"H": 1.5, "D": 4.0, "A": 6.0},
                      news_analyzer=lambda *a, **k: {
                          "home_goal_delta": 0.0, "away_goal_delta": 0.0,
                          "confidence": "low", "notes": [], "provider": None})
    pe = card["pick_exact_score"]
    ph, pa = int(pe["home"]), int(pe["away"])
    w, l = max(ph, pa), min(ph, pa)
    expected = exact_multiplier(STAGE_TYPE["QF"], w, l)
    assert abs(card["exact_multiplier_used"] - expected) < 1e-9
