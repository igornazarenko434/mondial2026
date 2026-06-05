"""Day-3 glue: historical results -> fitted Dixon-Coles strengths -> per-fixture
expected goals (which then feed blend.blended_matrix instead of the rough Elo prior).

Caches the fitted strengths so the daily refresh re-fits at most once a day.

Two fit backends:
  * **penaltyblog** (preferred): Cython-compiled likelihood, fits 4000+
    international results in ~8s. Used when the package is importable.
  * scipy SLSQP (fallback): the in-repo implementation. Correct but pure-Python
    — ~10-20 min at full international scale because finite-difference gradients
    over 514 parameters traverse all rows. Fine for small/synthetic datasets
    (tests, ad-hoc fits) but impractical for the real pipeline.

Both paths return the SAME schema so the rest of the codebase is unchanged:
    {"teams": {team: {"attack": float, "defence": float}}, "home_adv": float, "rho": float}
"""
from __future__ import annotations
import os
from core.models import dixon_coles
from core.data.cache import cached_json
from core.obs.logging import get_logger

log = get_logger("models.fit")
CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "store")


def _to_writable_numpy(df):
    """Pandas 3.0 Series expose read-only buffers; penaltyblog's Cython binding
    requires writable arrays. Explicit copy → defeats the buffer-source error."""
    import numpy as np
    return (
        np.asarray(df["home_goals"].to_numpy(), dtype=np.int64).copy(),
        np.asarray(df["away_goals"].to_numpy(), dtype=np.int64).copy(),
        df["home"].astype(str).to_numpy().copy(),
        df["away"].astype(str).to_numpy().copy(),
    )


def _fit_with_penaltyblog(rows: list[dict]) -> dict:
    """Fast path. Returns the canonical {teams, home_adv, rho} dict."""
    import pandas as pd
    import penaltyblog as pb
    df = pd.DataFrame(rows)
    hg, ag, h, a = _to_writable_numpy(df)
    model = pb.models.DixonColesGoalModel(hg, ag, h, a)
    model.fit()
    p = model.params
    teams = sorted(set(h) | set(a))
    return {
        "teams": {t: {"attack": float(p[f"attack_{t}"]),
                       "defence": float(p[f"defence_{t}"])} for t in teams},
        "home_adv": float(p["home_advantage"]),
        "rho": float(p["rho"]),
    }


def _fit_with_scipy(rows: list[dict], xi: float) -> dict:
    """Slow but always-available fallback (the in-repo implementation)."""
    import pandas as pd
    df = pd.DataFrame(rows).rename(columns={"home": "home_team", "away": "away_team"})
    return dixon_coles.fit_strengths(df, xi=xi)


def fit_from_results(results: list[dict], xi: float = 0.0018):
    """Fit attack/defence/home-adv/rho from normalized result rows
    ({home, away, home_goals, away_goals, days_ago}).

    Prefers penaltyblog (fast Cython); falls back to scipy SLSQP if not installed.
    xi (time-decay) is honored only in the scipy path; the penaltyblog path uses
    uniform weights — acceptable given the 4-year results window already filters
    aggressively for tactical relevance.
    """
    try:
        import penaltyblog  # noqa: F401 - presence check
        return _fit_with_penaltyblog(results)
    except ImportError:
        log.info("penaltyblog not installed; using scipy fallback "
                 "(slow at international scale)")
        return _fit_with_scipy(results, xi)


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
