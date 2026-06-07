"""Scoring engine asserted against the worked examples in the rules PDF."""
from core.scoring.engine import score_match, direction, apply_group_reset, prize_split

ODDS = {"H": 2.0, "D": 2.5, "A": 1.5}   # France(home) 2.0 / draw 2.5 / Spain 1.5


def test_direction():
    assert direction(2, 1) == "H"
    assert direction(1, 1) == "D"
    assert direction(0, 2) == "A"


def test_group_exact_2_1():
    # §12b.iii: France 2-1 -> 1.5 * 2.0 = 3.000
    assert score_match("Group", 2, 1, 2, 1, ODDS) == 3.0


def test_group_direction_only():
    # §12b.ii: predict France 2-1, actual France 3-0 (right dir, not exact) -> 1.0 * 2.0 = 2.0
    assert score_match("Group", 2, 1, 3, 0, ODDS) == 2.0


def test_group_draw_1_1():
    # §12c.ii: draw 1-1 -> 2.25 * 2.5 = 5.625
    assert score_match("Group", 1, 1, 1, 1, ODDS) == 5.625


def test_group_draw_not_1_1():
    # §12c.i: predict 1-1, actual 2-2 (right dir draw, not exact) -> 1.0 * 2.5 = 2.5
    assert score_match("Group", 1, 1, 2, 2, ODDS) == 2.5


def test_group_exact_1_0_negev_aligned():
    """Day-9.7 fix: group-stage 1-0 multiplier = 1.5 (matches Negev's
    server-side scoring grid). Previously was 2.25 in our table —
    off-by-one transcription from the PDF clean-sheet row."""
    assert score_match("Group", 1, 0, 1, 0, ODDS) == 1.5 * 2.0   # = 3.0


def test_group_exact_2_0_negev_aligned():
    """Day-9.7 fix: 2-0 = 2.25 (was 3.25)."""
    assert score_match("Group", 2, 0, 2, 0, ODDS) == 2.25 * 2.0  # = 4.5


def test_group_exact_3_0_negev_aligned():
    """Day-9.7 fix: 3-0 = 3.25 (was 4.5)."""
    assert score_match("Group", 3, 0, 3, 0, ODDS) == 3.25 * 2.0  # = 6.5


def test_wrong_direction_zero():
    assert score_match("Group", 2, 1, 0, 1, ODDS) == 0.0


def test_detonator_doubles():
    assert score_match("Group", 2, 1, 2, 1, ODDS, detonator=True) == 6.0


def test_final_draw_2_2():
    # §16d: final 2-2 exact -> table 5 * draw odds
    assert score_match("Final", 2, 2, 2, 2, ODDS) == 5 * 2.5


def test_knockout_base_is_1_5():
    # right direction, not exact, R16 -> 1.5 * odds
    assert score_match("R16", 1, 0, 2, 0, ODDS) == 1.5 * 2.0


def test_group_reset():
    assert apply_group_reset(100) == 85.0


def test_prize_split():
    p = prize_split(1000)
    assert p[1] == 230.0 and p[2] == 150.0 and p[10] == 40.0


def test_unknown_stage_raises_clear_error():
    import pytest
    with pytest.raises(ValueError):
        score_match("LAST_64", 1, 0, 1, 0, ODDS)     # unmapped stage -> clear ValueError
