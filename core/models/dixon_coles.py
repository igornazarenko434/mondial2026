"""Dixon-Coles bivariate-Poisson score model.

`score_matrix()` is fully working: give it expected goals for each side and it
returns the (n+1)x(n+1) probability matrix with the Dixon-Coles low-score
correction. `fit_strengths()` is a working maximum-likelihood fit on a results
DataFrame (needs scipy); wire it to real international results in the Data
agent. See https://pena.lt/y/2021/06/24/ for the reference implementation.
"""
from __future__ import annotations
import numpy as np
from math import exp, log, lgamma


def _poisson(k: int, lam: float) -> float:
    """Numerically stable Poisson pmf (log-space) — safe for large goal counts
    and large lambda during optimization (no lam**k / factorial overflow)."""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return exp(k * log(lam) - lam - lgamma(k + 1))


def _dc_tau(i: int, j: int, lam: float, mu: float, rho: float) -> float:
    """Dixon-Coles correction for 0-0, 1-0, 0-1, 1-1."""
    if i == 0 and j == 0:
        return 1 - lam * mu * rho
    if i == 0 and j == 1:
        return 1 + lam * rho
    if i == 1 and j == 0:
        return 1 + mu * rho
    if i == 1 and j == 1:
        return 1 - rho
    return 1.0


def score_matrix(exp_home: float, exp_away: float,
                 rho: float = -0.13, max_goals: int = 8) -> np.ndarray:
    """Probability matrix M[i, j] = P(home i goals, away j goals)."""
    m = np.zeros((max_goals + 1, max_goals + 1))
    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            m[i, j] = (_poisson(i, exp_home) * _poisson(j, exp_away)
                       * _dc_tau(i, j, exp_home, exp_away, rho))
    return m / m.sum()


def fit_strengths(results_df, xi: float = 0.0):
    """Maximum-likelihood fit of per-team attack/defence + home advantage.

    results_df columns: home_team, away_team, home_goals, away_goals, [days_ago]
    Returns dict {team: {"attack":, "defence":}, "home_adv":, "rho":}.
    Time-decay weight = exp(-xi * days_ago) if `days_ago` present (xi>0).

    TODO(day3): call this with real international results pulled by the Data
    agent, then feed expected goals into score_matrix(). Optionally swap in
    `penaltyblog` which provides a tested DC fitter.
    """
    from scipy.optimize import minimize  # local import: only needed for fit
    teams = sorted(set(results_df.home_team) | set(results_df.away_team))
    idx = {t: i for i, t in enumerate(teams)}
    n = len(teams)

    def neg_log_like(params):
        atk = params[:n]
        dfc = params[n:2 * n]
        home_adv, rho = params[2 * n], params[2 * n + 1]
        ll = 0.0
        for r in results_df.itertuples():
            lam = exp(atk[idx[r.home_team]] + dfc[idx[r.away_team]] + home_adv)
            mu = exp(atk[idx[r.away_team]] + dfc[idx[r.home_team]])
            w = exp(-xi * getattr(r, "days_ago", 0)) if xi else 1.0
            tau = _dc_tau(r.home_goals, r.away_goals, lam, mu, rho)
            ll += w * (log(_poisson(r.home_goals, lam) + 1e-12)
                       + log(_poisson(r.away_goals, mu) + 1e-12)
                       + log(max(1e-12, tau)))      # floor: tau can go <=0 for some rho
        return -ll

    x0 = np.concatenate([np.zeros(n), np.zeros(n), [0.25, -0.1]])
    cons = {"type": "eq", "fun": lambda p: p[:n].sum()}  # identifiability
    # bounds keep the fit well-behaved: strengths in [-3,3], home adv [-1,2], rho [-0.2,0.2]
    bounds = [(-3, 3)] * (2 * n) + [(-1, 2), (-0.2, 0.2)]
    res = minimize(neg_log_like, x0, bounds=bounds, constraints=cons, method="SLSQP")
    p = res.x
    return {
        "teams": {t: {"attack": p[idx[t]], "defence": p[n + idx[t]]} for t in teams},
        "home_adv": p[2 * n], "rho": p[2 * n + 1],
    }


def expected_goals(strengths: dict, home: str, away: str) -> tuple[float, float]:
    """Expected goals for each side from fitted strengths."""
    t = strengths["teams"]
    lam = exp(t[home]["attack"] + t[away]["defence"] + strengths["home_adv"])
    mu = exp(t[away]["attack"] + t[home]["defence"])
    return lam, mu
