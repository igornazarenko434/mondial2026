"""National-team Elo -> win/draw/away probabilities and expected goal diff.

Working math; plug real Elo numbers from eloratings.net (pulled by the Data
agent). Draw probability uses a standard logistic-of-rating-gap heuristic that
you can recalibrate on historical international results.
"""
from __future__ import annotations
import math


def expected_result(home_elo: float, away_elo: float,
                    home_field: float = 60.0) -> float:
    """Elo expected score for the home side (0..1)."""
    diff = (home_elo + home_field) - away_elo
    return 1.0 / (1.0 + 10 ** (-diff / 400.0))


def outcome_probs(home_elo: float, away_elo: float,
                  home_field: float = 60.0, draw_base: float = 0.27) -> dict:
    """Approximate P(home/draw/away). draw_base shrinks as the gap widens."""
    e_home = expected_result(home_elo, away_elo, home_field)
    gap = abs(e_home - 0.5)
    p_draw = max(0.06, draw_base * (1 - gap))         # fewer draws in mismatches
    # split the remaining mass around the Elo expectation
    p_home = e_home - p_draw / 2
    p_away = (1 - e_home) - p_draw / 2
    p_home, p_away = max(p_home, 0.01), max(p_away, 0.01)
    s = p_home + p_draw + p_away
    return {"H": p_home / s, "D": p_draw / s, "A": p_away / s}


def expected_goals_from_elo(home_elo: float, away_elo: float,
                            base_total: float = 2.6) -> tuple[float, float]:
    """Rough expected-goals split implied by the Elo gap; a prior for the blend."""
    e_home = expected_result(home_elo, away_elo)
    home_share = 0.5 + (e_home - 0.5) * 0.8
    return base_total * home_share, base_total * (1 - home_share)
