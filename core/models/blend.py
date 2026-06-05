"""Blend the three estimates into one score matrix.

Strategy: build a score matrix from Dixon-Coles expected goals, scale its
direction mass toward the Elo + market consensus (market-leaning weights), and
renormalise. The result feeds the EV optimizer.
"""
from __future__ import annotations
import numpy as np
from config.rules import BLEND_WEIGHTS
from core.models.dixon_coles import score_matrix
from core.scoring.engine import direction, direction_probs


def blended_matrix(exp_home: float, exp_away: float,
                   elo_probs: dict, market_probs: dict,
                   weights: dict | None = None, max_goals: int = 8) -> np.ndarray:
    """Combine DC shape with Elo+market direction probabilities."""
    w = weights or BLEND_WEIGHTS
    base = score_matrix(exp_home, exp_away, max_goals=max_goals)
    dc = direction_probs(base)

    # target direction probabilities = weighted average of the three sources
    target = {}
    for d in ("H", "D", "A"):
        target[d] = (w["dixon_coles"] * dc[d]
                     + w["elo"] * elo_probs.get(d, dc[d])
                     + w["market"] * market_probs.get(d, dc[d]))
    s = sum(target.values())
    target = {d: v / s for d, v in target.items()}

    # re-weight each cell so its direction mass matches the target
    n = base.shape[0]
    out = np.zeros_like(base)
    for i in range(n):
        for j in range(n):
            d = direction(i, j)
            if dc[d] > 0:
                out[i, j] = base[i, j] * (target[d] / dc[d])
    return out / out.sum()
