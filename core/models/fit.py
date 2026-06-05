"""Day-3 glue: historical results -> fitted Dixon-Coles strengths -> per-fixture
expected goals (which then feed blend.blended_matrix instead of the rough Elo prior).

Caches the fitted strengths so the daily refresh re-fits at most once a day.
"""
from __future__ import annotations
import os
from core.models import dixon_coles
from core.data.cache import cached_json

CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "store")


def fit_from_results(results: list[dict], xi: float = 0.0018):
    """Fit attack/defence/home-adv/rho from normalized result rows
    ({home, away, home_goals, away_goals, days_ago}). Needs pandas + scipy.
    xi default ≈ a ~1-year half-life time-decay (recent matches weighted more)."""
    import pandas as pd
    df = pd.DataFrame(results).rename(columns={"home": "home_team", "away": "away_team"})
    return dixon_coles.fit_strengths(df, xi=xi)


def expected_goals_fn(strengths):
    """Return a function (home, away) -> (exp_home, exp_away) from fitted strengths,
    with a safe fallback for teams missing from the fit."""
    teams = strengths["teams"]

    def fn(home: str, away: str):
        if home in teams and away in teams:
            return dixon_coles.expected_goals(strengths, home, away)
        return 1.3, 1.1          # neutral fallback if a team wasn't in the training set
    return fn


def cached_strengths(results: list[dict], cache_path: str | None = None,
                     ttl_hours: float = 24, xi: float = 0.0018):
    """Fit once per day; cache the fitted strengths to JSON."""
    path = os.path.join(CACHE_DIR, "dc_strengths.json") if cache_path is None else cache_path
    return cached_json(path, ttl_hours, lambda: fit_from_results(results, xi=xi))
