"""Futures via Monte-Carlo tournament simulation (run once before 11.06 21:59).

TODO(day7): simulate all 104 matches from the match model:
  group stage -> rank within groups -> pick 8 best 3rd-placed teams ->
  build the round-of-32 bracket -> knockouts. Repeat N times.
Aggregate:
  P(team wins title)            -> EV vs config.rules.WINNER_PAYOUT   (§7)
  P(team reaches R16/QF/SF+)    -> EV vs CINDERELLA_PAYOUT / fighter   (§9,§10)
  expected goals per player     -> EV vs SCORER_PAYOUT                 (§8)

Use core.models.blend.blended_matrix to sample each match, then np.random
to draw a scoreline from the matrix. Reuse config.rules payouts for EV ranking.
"""
from __future__ import annotations
import numpy as np


def sample_score(matrix: np.ndarray, rng: np.random.Generator) -> tuple[int, int]:
    """Draw one (home, away) scoreline from a probability matrix."""
    flat = matrix.ravel()
    k = rng.choice(len(flat), p=flat)
    return int(k // matrix.shape[1]), int(k % matrix.shape[1])


def simulate_tournament(n: int = 10000):
    """TODO: implement full bracket sim and return EV tables per futures market."""
    raise NotImplementedError("Day 7 — see CLAUDE.md build order.")
