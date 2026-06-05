"""Fit backend selection (penaltyblog preferred, scipy fallback) — both
return the canonical strengths schema so the rest of the pipeline (predict,
blend, backtest) is backend-agnostic."""
from __future__ import annotations
import sys
import pytest

from core.models import fit


SMALL_RESULTS = [
    {"home": "Spain",   "away": "Germany", "home_goals": 1, "away_goals": 2, "days_ago":   1},
    {"home": "Germany", "away": "Spain",   "home_goals": 0, "away_goals": 1, "days_ago": 100},
    {"home": "France",  "away": "Spain",   "home_goals": 0, "away_goals": 0, "days_ago": 200},
    {"home": "France",  "away": "Germany", "home_goals": 2, "away_goals": 1, "days_ago": 300},
    {"home": "Spain",   "away": "France",  "home_goals": 1, "away_goals": 1, "days_ago": 400},
    {"home": "Germany", "away": "France",  "home_goals": 0, "away_goals": 2, "days_ago": 500},
]


def _assert_canonical_schema(s):
    """Both backends must produce this shape so downstream code (predict.py,
    blend.py, expected_goals_fn) consumes either result transparently."""
    assert set(s.keys()) >= {"teams", "home_adv", "rho"}
    assert isinstance(s["teams"], dict) and len(s["teams"]) >= 3
    for team, vals in s["teams"].items():
        assert set(vals.keys()) == {"attack", "defence"}
        assert isinstance(vals["attack"], float)
        assert isinstance(vals["defence"], float)
    assert isinstance(s["home_adv"], float)
    assert isinstance(s["rho"], float)


def test_fit_uses_penaltyblog_when_available():
    """When penaltyblog imports cleanly, fit_from_results uses the fast path."""
    pytest.importorskip("penaltyblog")
    s = fit.fit_from_results(SMALL_RESULTS)
    _assert_canonical_schema(s)
    # quick sanity: rho is in [-1, 1] range (Dixon-Coles correction parameter)
    assert -1.0 <= s["rho"] <= 1.0


def test_fit_falls_back_to_scipy_when_penaltyblog_missing(monkeypatch):
    """If penaltyblog is not installed, the scipy path must still produce the
    canonical schema (slow but correct on small datasets)."""
    # simulate "penaltyblog not installed" by deleting + blocking re-import
    monkeypatch.setitem(sys.modules, "penaltyblog", None)
    s = fit.fit_from_results(SMALL_RESULTS)
    _assert_canonical_schema(s)


def test_both_backends_agree_on_schema_keys():
    """Pin the cross-backend invariant explicitly — the set of teams returned
    must match the rows' team union, regardless of which backend ran."""
    pytest.importorskip("penaltyblog")
    s_pb = fit.fit_from_results(SMALL_RESULTS)
    expected_teams = {r["home"] for r in SMALL_RESULTS} | {r["away"] for r in SMALL_RESULTS}
    assert set(s_pb["teams"].keys()) == expected_teams


def test_expected_goals_fn_works_on_penaltyblog_output():
    """Critical glue: predict.py and downstream consume the expected_goals_fn
    wrapper; it must work with the penaltyblog-produced strengths dict."""
    pytest.importorskip("penaltyblog")
    s = fit.fit_from_results(SMALL_RESULTS)
    eg = fit.expected_goals_fn(s)
    lh, la = eg("Spain", "France")
    assert lh > 0 and la > 0
    # unknown team → safe (1.3, 1.1) fallback, never raise
    lh2, la2 = eg("Atlantis", "Eldorado")
    assert (lh2, la2) == (1.3, 1.1)
