"""The model→decision assembler — the single, explicit path from inputs to a card.

This is how the data flow is meant to look: each agent's output is a plain input
here, and this function fuses them into P(scoreline) and the EV-optimal pick. It is
degradation-aware (never raises) and matches the graceful-degradation ladder:
   model + odds + news  ->  model-only (no odds)  ->  modal pick (no usable odds)

Inputs (each produced by an agent):
  expected_goals_fn  : from the fitted Dixon-Coles (core/models/fit) — (home,away)->(λh,λa)
  elo                : {team: rating}        (data agent, eloratings)
  scoring_odds       : {"H","D","A"} decimal (odds agent — the points multiplier)
  news_deltas        : (home_delta, away_delta) bounded ±0.6 (news agent)
"""
from __future__ import annotations
import numpy as np
from core.models.elo import outcome_probs
from core.models.blend import blended_matrix
from core.data.oddsapi import devig
from core.data.soccerdata_io import elo_of
from core.decision.ev_optimizer import recommend
from core.scoring.engine import direction_probs


def score_distribution(home: str, away: str, expected_goals_fn, elo: dict,
                       scoring_odds: dict | None, news_deltas=(0.0, 0.0)) -> np.ndarray:
    """Fuse fitted goals + news + Elo + market into one P(scoreline) matrix."""
    lh, la = expected_goals_fn(home, away)
    lh = max(0.05, lh + news_deltas[0])
    la = max(0.05, la + news_deltas[1])
    elo_p = outcome_probs(elo_of(elo, home), elo_of(elo, away))
    try:
        market_p = devig(scoring_odds) if scoring_odds else elo_p
    except ValueError:
        market_p = elo_p                              # bad odds → model-only probability
    return blended_matrix(lh, la, elo_p, market_p)


def match_card(home: str, away: str, stage: str, detonator: bool,
               expected_goals_fn, elo: dict, scoring_odds: dict | None,
               news_deltas=(0.0, 0.0)) -> dict:
    """Full per-game card. Degradation-safe: with no usable odds it can't EV-optimize
    (points = base×odds), so it falls back to the most-likely (modal) score."""
    matrix = score_distribution(home, away, expected_goals_fn, elo, scoring_odds, news_deltas)
    valid_odds = bool(scoring_odds) and all(
        isinstance(scoring_odds.get(k), (int, float)) and scoring_odds[k] > 1 for k in ("H", "D", "A"))
    if valid_odds:
        card = recommend(matrix, stage, scoring_odds, detonator=detonator)
    else:                                             # no multiplier → modal pick
        i, j = np.unravel_index(int(np.argmax(matrix)), matrix.shape)
        card = {"pick_exact_score": {"home": int(i), "away": int(j)},
                "pick_direction": ("H" if i > j else "D" if i == j else "A"),
                "expected_points": None, "modal_score": {"home": int(i), "away": int(j)},
                "model_prob": {k: round(v, 3) for k, v in direction_probs(matrix).items()},
                "ranked_alternatives": [], "detonator": detonator, "locked_odds": scoring_odds,
                "note": "no usable odds — modal pick (cannot EV-optimize)"}
    card.update({"home": home, "away": away, "stage": stage})
    # Day-9.26.2: news/disagreement rendering moved to delivery.base.render_card
    # so the line ALWAYS shows (with confidence + scale annotation), even when
    # both deltas are 0.0 — visibility was the operator's main complaint after
    # USA-Paraguay's silent news section.
    return card
