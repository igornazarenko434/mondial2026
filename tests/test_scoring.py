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
    # §16d: Day-9.33 (2026-06-29) — Negev re-priced semiAndFinal to 0.75×
    # the legacy schedule. Final 2-2 cell: was 5, now 3.75.
    assert score_match("Final", 2, 2, 2, 2, ODDS) == 3.75 * 2.5


def test_knockout_base_is_1_5():
    # right direction, not exact, R16 -> 1.5 * odds
    assert score_match("R16", 1, 0, 2, 0, ODDS) == 1.5 * 2.0


def test_group_reset():
    assert apply_group_reset(100) == 85.0


def test_prize_split():
    p = prize_split(1000)
    assert p[1] == 230.0 and p[2] == 150.0 and p[10] == 40.0


# Day-9.33: cell-by-cell pin of every Negev-aligned multiplier. If a future
# edit to config/rules.py reverts these accidentally, this test fails LOUDLY
# with a per-cell mismatch — the cron audit_negev_multipliers fires too but
# only once a day; this pins compile-time correctness.
import pytest

# Source of truth: tools/audit_negev_multipliers.py pulls these from Negev's
# server-side managerTables/grids on 2026-06-29. Anything that diverges
# from these constants without updating the cron audit too is a bug.
NEGEV_GROUP_STAGE = {
    (0, 0): 2.75, (1, 0): 1.5, (1, 1): 2.25, (2, 0): 2.25, (2, 1): 1.5,
    (2, 2): 2.75, (3, 0): 3.25, (3, 1): 3.25, (3, 2): 3.25, (3, 3): 4.5,
    (4, 0): 4.5, (4, 1): 4.5, (4, 2): 4.5,
}
NEGEV_R16_AND_QF = {
    (0, 0): 3.75, (1, 0): 2.25, (1, 1): 3.0, (2, 0): 3.5, (2, 1): 2.25,
    (2, 2): 3.75, (3, 0): 4.5, (3, 1): 4.5, (3, 2): 4.5, (3, 3): 8.25,
}
# Day-9.33 RE-PRICED — only the LEGACY values would be 5/3/4.5/6/11/etc.
# Note: cells with winner_goals >= 4 LOSE their explicit entry and fall back
# to TABLE_CAP["final"]=11 in SCORE_TABLE (see _to_dict + exact_multiplier).
# Below pins only cells the table actually stores; the cap is tested separately.
NEGEV_SEMI_AND_FINAL = {
    (0, 0): 3.75, (1, 0): 2.25, (1, 1): 3.0, (2, 0): 3.5, (2, 1): 2.25,
    (2, 2): 3.75, (3, 0): 4.5, (3, 1): 4.5, (3, 2): 4.5, (3, 3): 8.25,
    (4, 0): 8.25, (4, 1): 8.25, (4, 2): 8.25, (4, 3): 8.25, (4, 4): 8.25,
}


@pytest.mark.parametrize("key,expected", sorted(NEGEV_GROUP_STAGE.items()))
def test_group_grid_pinned_to_negev(key, expected):
    from config.rules import SCORE_TABLE
    w, l = key
    assert SCORE_TABLE["group"][(w, l)] == expected, (
        f"_GROUP[{l}][{w}] drifted vs Negev's groupStage — "
        f"got {SCORE_TABLE['group'][(w, l)]}, expected {expected}")


@pytest.mark.parametrize("key,expected", sorted(NEGEV_R16_AND_QF.items()))
def test_ko_grid_pinned_to_negev(key, expected):
    from config.rules import SCORE_TABLE
    w, l = key
    assert SCORE_TABLE["ko"][(w, l)] == expected, (
        f"_KO[{l}][{w}] drifted vs Negev's round16AndQuarter — "
        f"got {SCORE_TABLE['ko'][(w, l)]}, expected {expected}")


@pytest.mark.parametrize("key,expected", sorted(NEGEV_SEMI_AND_FINAL.items()))
def test_final_grid_pinned_to_negev_post_day_9_33(key, expected):
    from config.rules import SCORE_TABLE
    w, l = key
    assert SCORE_TABLE["final"][(w, l)] == expected, (
        f"_FINAL[{l}][{w}] drifted vs Negev's semiAndFinal — "
        f"got {SCORE_TABLE['final'][(w, l)]}, expected {expected}. "
        f"Day-9.33 lowered this cell; reverting is a bug.")


def test_final_blowout_cap_unchanged():
    """Blowout cap (cells with winner ≥ 5, or beyond the printed grid) is
    still 11 — Negev kept the cap; only the LOW-scoring cells dropped."""
    from config.rules import TABLE_CAP
    from core.scoring.engine import exact_multiplier
    assert TABLE_CAP["final"] == 11.0
    # exact_multiplier returns the cap for any cell not in the table
    assert exact_multiplier("final", 5, 0) == 11.0
    assert exact_multiplier("final", 7, 1) == 11.0
    assert exact_multiplier("final", 8, 8) == 11.0
    # And the cap for ko stays 8.25
    assert TABLE_CAP["ko"] == 8.25
    assert exact_multiplier("ko", 5, 0) == 8.25


def test_unknown_stage_raises_clear_error():
    import pytest
    with pytest.raises(ValueError):
        score_match("LAST_64", 1, 0, 1, 0, ODDS)     # unmapped stage -> clear ValueError
