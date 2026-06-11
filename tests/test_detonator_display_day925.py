"""Day-9.25: pin the detonator-display fix.

Live incident: the rendered card showed "Expected points ≈ 3.37 → ×2
detonator ≈ 6.74" for the Mexico v South Africa opener. But:
  - `rank_scores(detonator=True)` returns `expected_points` already
    multiplied by DETONATOR_FACTOR=2 (pinned by `test_detonator_doubles_ev`)
  - The render then multiplied that value by 2 AGAIN for display

→ The "6.74" line was 4× the true non-detonator EV, double-counting the
   ×2 factor in front of the user.

This test pins three properties:
  1. The numeric value displayed equals `expected_points` as-is (no doubling)
  2. The "(×2 detonator already applied)" annotation appears when detonator
  3. Non-detonator cards have no annotation
"""
from __future__ import annotations

import re
from core.delivery.base import render_card


def _card(**overrides):
    base = {
        "match_id": 537327, "home": "Mexico", "away": "South Africa",
        "stage": "Group", "group": "A", "detonator": True,
        "kickoff_local": "2026-06-11 22:00",
        "locked_odds": {"H": 1.43, "D": 4.42, "A": 8.78},
        "model_prob":  {"H": 0.67, "D": 0.22, "A": 0.10},
        "pick_exact_score": {"home": 0, "away": 0},
        "pick_direction": "D",
        "modal_score": {"home": 1, "away": 0},
        "expected_points": 3.37,        # already includes ×2 detonator
        "context": [],
        "signals_used":    ["dixon_coles", "elo", "market", "news"],
        "signals_failed":  [],
        "failure_reasons": {},
        "ev_pathway": "ev_optimized",
        "penalty_winner": None,
        "news_provider": "gemini",
    }
    base.update(overrides)
    return base


def test_detonator_card_shows_value_as_is_not_doubled():
    """Production card EV=3.37 must display as 3.37 — NOT 3.37 → 6.74."""
    txt = render_card(_card(detonator=True, expected_points=3.37))
    ev_lines = [ln for ln in txt.split("\n") if ln.startswith("Expected points")]
    assert len(ev_lines) == 1
    line = ev_lines[0]
    # The displayed value is 3.37, full stop.
    assert "≈ 3.37" in line, f"expected '≈ 3.37' in line, got: {line!r}"
    # No "→ ×2 detonator ≈ 6.74" arrow with a doubled value.
    assert "6.74" not in line, \
        f"detonator display double-counted (4× pre-detonator EV): {line!r}"


def test_detonator_card_shows_annotation_that_x2_already_applied():
    """User must know the ×2 is baked into the number (otherwise they'd
    expect to mentally double it themselves)."""
    txt = render_card(_card(detonator=True, expected_points=3.37))
    ev_lines = [ln for ln in txt.split("\n") if ln.startswith("Expected points")]
    assert "(×2 detonator already applied)" in ev_lines[0]


def test_nondetonator_card_has_no_detonator_annotation():
    """Regression: non-detonator cards keep their clean 'Expected points ≈
    X.XX' line — no annotation, no ×2 arrow."""
    txt = render_card(_card(detonator=False, expected_points=1.42))
    ev_lines = [ln for ln in txt.split("\n") if ln.startswith("Expected points")]
    line = ev_lines[0]
    assert "≈ 1.42" in line
    assert "detonator" not in line
    assert "×2" not in line
    assert "→" not in line


def test_no_naked_arrow_to_quadrupled_value_for_any_realistic_ev():
    """Stress: any positive expected_points must NOT produce a display whose
    second number is the input ev × 2."""
    for ev in (0.5, 1.0, 3.37, 5.0, 12.5):
        txt = render_card(_card(detonator=True, expected_points=ev))
        ev_lines = [ln for ln in txt.split("\n") if ln.startswith("Expected points")]
        line = ev_lines[0]
        # Detect a "→ ... ≈ <num>" pattern; assert the <num> is NOT 2×ev.
        m = re.search(r"≈\s+([0-9]+\.[0-9]+).*≈\s+([0-9]+\.[0-9]+)", line)
        if m:
            first, second = float(m.group(1)), float(m.group(2))
            assert abs(second - first * 2) > 1e-3, \
                f"display double-counts detonator: {line!r}"


def test_korea_card_no_detonator_displays_clean():
    """South Korea vs Czechia — no detonator — displays EV without any
    detonator-related text. Pins the Telegram-card the user actually saw."""
    card = _card(detonator=False,
                 home="South Korea", away="Czechia",
                 expected_points=1.41,
                 locked_odds={"H": 2.74, "D": 3.10, "A": 2.89},
                 model_prob={"H": 0.40, "D": 0.29, "A": 0.31},
                 pick_exact_score={"home": 1, "away": 1},
                 pick_direction="D",
                 modal_score={"home": 1, "away": 1})
    txt = render_card(card)
    ev_lines = [ln for ln in txt.split("\n") if ln.startswith("Expected points")]
    assert "≈ 1.41" in ev_lines[0]
    assert "detonator" not in ev_lines[0]
