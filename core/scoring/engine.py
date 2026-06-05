"""Scoring engine — the Python mirror of the spreadsheet.

`score_match` reproduces the friends' rules exactly. It is unit-tested against
the worked examples in the rules PDF (see tests/test_scoring.py):
    France win 2-1, odds 2.0  -> 1.5 * 2.0      = 3.000
    Draw 1-1,      odds 2.5  -> 2.25 * 2.5      = 5.625
    France win not-2-1, odds 2.0 -> 1.0 * 2.0   = 2.000
"""
from __future__ import annotations
from config.rules import (STAGE_TYPE, BASE_POINTS, SCORE_TABLE, TABLE_CAP,
                          DETONATOR_FACTOR, GROUP_RESET_FACTOR, PRIZE_LADDER)


def direction(home_goals: int, away_goals: int) -> str:
    """'H' home win, 'D' draw, 'A' away win."""
    if home_goals > away_goals:
        return "H"
    if home_goals == away_goals:
        return "D"
    return "A"


def direction_probs(matrix) -> dict:
    """Sum a score-probability matrix into P(home/draw/away). Single source of
    truth used by both the blend and the EV optimizer."""
    p = {"H": 0.0, "D": 0.0, "A": 0.0}
    n, m = matrix.shape[0], matrix.shape[1]
    for i in range(n):
        for j in range(m):
            p[direction(i, j)] += float(matrix[i][j])
    return p


def exact_multiplier(stage_type: str, winner_goals: int, loser_goals: int) -> float:
    """Table multiplier for an exact scoreline; falls back to the table cap
    for very rare scorelines beyond the printed grid."""
    return SCORE_TABLE[stage_type].get((winner_goals, loser_goals),
                                       TABLE_CAP[stage_type])


def score_match(stage: str, pred_h: int, pred_a: int,
                act_h: int, act_a: int, odds: dict,
                detonator: bool = False) -> float:
    """Points for one match.

    odds: {"H": float, "D": float, "A": float} -- the LOCKED bookmaker odds
          (these are the scoring multiplier per the rules).
    """
    stype = STAGE_TYPE.get(stage)
    if stype is None:
        raise ValueError(f"unknown stage '{stage}'; add it to config.rules.STAGE_TYPE "
                         f"(valid: {sorted(STAGE_TYPE)})")
    base = BASE_POINTS[stype]

    dir_pred = direction(pred_h, pred_a)
    dir_act = direction(act_h, act_a)
    if dir_pred != dir_act:                       # wrong direction -> 0
        return 0.0

    odds_used = odds[dir_act]
    is_exact = (pred_h == act_h and pred_a == act_a)
    if is_exact:
        w, l = max(act_h, act_a), min(act_h, act_a)
        mult = exact_multiplier(stype, w, l)
    else:
        mult = base

    pts = mult * odds_used
    if detonator:
        pts *= DETONATOR_FACTOR
    return round(pts, 3)


# --- Standings helpers ------------------------------------------------------
def apply_group_reset(group_points: float) -> float:
    """§14: cut every participant's group-stage total by 15%."""
    return round(group_points * GROUP_RESET_FACTOR, 3)


def prize_split(total_pot: float, n_ranked: int = 10) -> dict[int, float]:
    """Money per finishing place from the §5 ladder."""
    return {rank: round(total_pot * pct, 2)
            for rank, pct in PRIZE_LADDER.items() if rank <= n_ranked}
