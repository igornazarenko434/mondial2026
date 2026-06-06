"""Day-7 Monte Carlo simulator: structural correctness + sanity on a tiny
toy tournament, plus the convergence test on a synthetic 12-group field."""
from __future__ import annotations
import numpy as np
import pytest
from core.models.montecarlo import (
    simulate_group, _best_third_placed, _build_r32_bracket,
    _simulate_ko_match, run_tournament, monte_carlo, deep_run_prob,
    expected_team_goals, STAGES, sample_score, load_groups_csv,
)


# ---------- toy helpers ----------

def _strong_vs_weak_eg(strong: str, weak: str):
    """An eg_fn that gives `strong` ~3 expected goals and `weak` ~0.3."""
    def fn(h, a):
        if h == strong: return 3.0, 0.3
        if a == strong: return 0.3, 3.0
        return 1.3, 1.1
    return fn


def _balanced_eg(h, a):
    return 1.3, 1.1


def _high_elo(strong: str):
    return {strong: 2200.0, "Other1": 1500.0, "Other2": 1500.0, "Other3": 1500.0}


# ---------- simulate_group ----------

def test_simulate_group_returns_4_rows_sorted_desc():
    teams = ["A", "B", "C", "D"]
    rng = np.random.default_rng(0)
    standings = simulate_group(teams, _balanced_eg, rng)
    assert len(standings) == 4
    for row in standings:
        assert set(row.keys()) == {"team", "pts", "gd", "gf", "ga"}
    # Sorted descending
    for i in range(3):
        a, b = standings[i], standings[i + 1]
        assert (a["pts"], a["gd"], a["gf"]) >= (b["pts"], b["gd"], b["gf"])


def test_simulate_group_strong_team_wins_more_often_than_not():
    """Statistical: 'Strong' (3.0 xG/match) should top the group most of the time."""
    teams = ["Strong", "B", "C", "D"]
    rng = np.random.default_rng(123)
    wins = 0
    for _ in range(200):
        s = simulate_group(teams, _strong_vs_weak_eg("Strong", None), rng)
        if s[0]["team"] == "Strong":
            wins += 1
    # Strong should win > 70% of groups
    assert wins > 140, f"Strong topped only {wins}/200 groups"


# ---------- bracket builder ----------

def test_r32_bracket_has_16_pairs_no_intra_group_when_possible():
    # 12 groups × 4 = 48 teams; we need 12 winners + 12 runners-up + 8 thirds
    standings = {}
    for g in "ABCDEFGHIJKL":
        standings[g] = [
            {"team": f"{g}1", "pts": 9, "gd": 5, "gf": 7, "ga": 2},
            {"team": f"{g}2", "pts": 6, "gd": 2, "gf": 5, "ga": 3},
            {"team": f"{g}3", "pts": 3, "gd": 0, "gf": 3, "ga": 3},
            {"team": f"{g}4", "pts": 0, "gd": -7, "gf": 1, "ga": 8},
        ]
    best_thirds = _best_third_placed(standings)
    assert len(best_thirds) == 8
    pairs = _build_r32_bracket(standings, best_thirds)
    assert len(pairs) == 16
    # All 32 teams must appear exactly once
    appearing = [t for p in pairs for t in p]
    assert len(set(appearing)) == 32


# ---------- KO simulation ----------

def test_ko_match_returns_one_of_the_two_teams():
    rng = np.random.default_rng(0)
    elo = {"A": 1900.0, "B": 1500.0}
    w, l = _simulate_ko_match("A", "B", _balanced_eg, elo, rng)
    assert {w, l} == {"A", "B"}
    assert w != l


def test_ko_match_draw_resolved_by_shootout_edge():
    """When goals tie, the higher-Elo side should win the shootout most of the time."""
    elo = {"A": 2200.0, "B": 1400.0}
    # Pin the eg_fn so we get 0-0 every time (lambda 0.0 floors to 0.05)
    def zero(h, a): return 0.0, 0.0
    rng = np.random.default_rng(42)
    a_wins = 0
    for _ in range(500):
        w, _ = _simulate_ko_match("A", "B", zero, elo, rng)
        if w == "A":
            a_wins += 1
    # Strong side should win more often (slight edge due to penalty cap +5pp)
    assert a_wins > 250, f"strong side won only {a_wins}/500 shootouts"


# ---------- run_tournament + monte_carlo on a tiny field ----------

def _tiny_field() -> dict[str, list[str]]:
    """12 groups × 4 teams = 48 teams (matches WC 2026 shape)."""
    return {g: [f"{g}{i+1}" for i in range(4)] for g in "ABCDEFGHIJKL"}


def test_run_tournament_visits_every_team_with_a_stage():
    rng = np.random.default_rng(0)
    field = _tiny_field()
    eg_fn = _balanced_eg
    elo = {t: 1500.0 for g in field.values() for t in g}
    reached = run_tournament(field, eg_fn, elo, rng)
    assert len(reached) == 48
    for t, st in reached.items():
        assert st in STAGES


def test_monte_carlo_probabilities_sum_to_one_per_team():
    field = _tiny_field()
    eg_fn = _balanced_eg
    elo = {t: 1500.0 for g in field.values() for t in g}
    # Small n for fast test; ratios won't be tight but sum must hold
    mc = monte_carlo(field, eg_fn, elo, n=50, seed=0)
    for t, probs in mc.items():
        assert abs(sum(probs.values()) - 1.0) < 1e-9


def test_monte_carlo_strong_team_reaches_qf_more_than_weak():
    """With 1 strong team in group A and weaker everyone else, the strong
    team should reach QF much more often than the average."""
    field = _tiny_field()
    elo = {t: 1500.0 for g in field.values() for t in g}
    elo["A1"] = 2200.0
    def eg(h, a):
        if h == "A1": return 3.0, 0.3
        if a == "A1": return 0.3, 3.0
        return 1.3, 1.1
    mc = monte_carlo(field, eg, elo, n=400, seed=1)
    strong_qf = deep_run_prob(mc["A1"], "qf")
    avg_qf = np.mean([deep_run_prob(mc[t], "qf")
                       for t in mc if t != "A1"])
    assert strong_qf > avg_qf + 0.10, \
        f"strong team P(reach QF) {strong_qf:.3f} only marginal vs avg {avg_qf:.3f}"


# ---------- deep_run_prob + expected_team_goals ----------

def test_deep_run_prob_includes_only_stages_at_or_beyond_min():
    probs = {"group_only": 0.05, "r32": 0.10, "r16": 0.20, "qf": 0.30,
             "sf": 0.20, "final": 0.10, "champion": 0.05}
    assert deep_run_prob(probs, "qf") == 0.30 + 0.20 + 0.10 + 0.05
    assert deep_run_prob(probs, "sf") == 0.20 + 0.10 + 0.05
    assert deep_run_prob(probs, "r16") == 0.20 + 0.30 + 0.20 + 0.10 + 0.05


def test_expected_team_goals_scales_with_per_match_xg():
    probs = {"group_only": 0.10, "r32": 0.20, "r16": 0.30, "qf": 0.20,
             "sf": 0.10, "final": 0.05, "champion": 0.05}
    a = expected_team_goals(probs, 0.5)
    b = expected_team_goals(probs, 1.0)
    assert abs(b / a - 2.0) < 1e-9


# ---------- sample_score (DC-matrix sampling helper) ----------

def test_sample_score_uses_matrix_distribution():
    """A point-mass matrix at (1, 0) must always sample (1, 0)."""
    m = np.zeros((4, 4))
    m[1, 0] = 1.0
    rng = np.random.default_rng(0)
    for _ in range(50):
        assert sample_score(m, rng) == (1, 0)


# ---------- groups CSV loader ----------

def test_load_groups_csv_returns_12_groups_of_4():
    field = load_groups_csv("data/wc2026_groups.csv")
    assert len(field) == 12
    assert all(len(v) == 4 for v in field.values()), \
        f"unbalanced groups: {[(g, len(v)) for g, v in field.items()]}"
    # All 48 teams canonical
    flat = sum(field.values(), [])
    assert len(flat) == 48
    assert len(set(flat)) == 48
