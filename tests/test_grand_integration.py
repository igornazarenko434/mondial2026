"""Whole-system integration: pre-tournament futures + daily side bets + per-game
pipeline (strategy OFF and ON) + degradation + loud failure — together, no network.
"""
import sqlite3
from datetime import datetime, timezone, timedelta
import numpy as np

from core import obs
from core.obs.cost import CostLedger
from core.obs.runs import RunLedger
from core.decision.futures import implied_probs, recommend_futures
from core.decision.sidebets import recommend_total_goals
from core.models.dixon_coles import score_matrix
from core.models.elo import outcome_probs, expected_goals_from_elo
from core.models.blend import blended_matrix
from core.data.oddsapi import devig
from core.decision.ev_optimizer import recommend
from orchestrator.pipeline import process_match


def _isolate(monkeypatch):
    led, rled = CostLedger(":memory:"), RunLedger(":memory:")
    monkeypatch.setattr("core.obs.cost._LEDGER", led, raising=False)
    monkeypatch.setattr("core.obs.runs._LEDGER", rled, raising=False)
    captured = []
    import core.delivery as d
    monkeypatch.setattr(d, "deliver_card", lambda c: captured.append(c) or True)
    monkeypatch.setattr(d, "alert", lambda t, b: captured.append(("ALERT", t, b)) or True)
    return led, rled, captured


def _build(match):
    """Degradation-aware build: odds → model → decision; if odds garbage, model-only."""
    with obs.staged("odds"):
        try:
            market = devig(match.get("odds") or {})
        except ValueError:
            market = None                      # degrade: no market signal
    with obs.staged("model"):
        elo_p = outcome_probs(1840, 2050)
        eh, ea = expected_goals_from_elo(1840, 2050)
        matrix = blended_matrix(eh, ea, elo_p, market or elo_p)
    with obs.staged("decision"):
        odds = match.get("odds") or {"H": 2.0, "D": 3.0, "A": 4.0}
        rec = recommend(matrix, match.get("stage", "Group"), odds,
                        detonator=bool(match.get("detonator")))
    rec.update({"home": match["home"], "away": match["away"], "stage": match.get("stage")})
    return rec


# ---------- 1. PRE-TOURNAMENT futures (the bets locked before kickoff) ----------
def test_pretournament_futures_from_market_odds():
    winner_odds = {"Spain": 6.0, "France": 6.5, "Brazil": 9.0, "United States": 200.0}
    out = recommend_futures({
        "winner": implied_probs(winner_odds),
        "cinderella": {"Jordan": 0.08, "Curacao": 0.03},
    })
    # EV-max accounts for PAYOUTS: Brazil (payout 33) beats Spain (20) here because
    # Brazil's implied prob is still solid → higher EV. This is the intended edge.
    tbl = {r["option"]: r["ev"] for r in out["tables"]["winner"]}
    assert out["picks"]["winner"] == max(tbl, key=tbl.get) == "Brazil"
    assert "cinderella" in out["picks"]


# ---------- 2. DAILY side bet ----------
def test_daily_side_bet():
    ms = [score_matrix(1.4, 1.1), score_matrix(1.6, 1.2)]
    rec = recommend_total_goals(ms, line=8.5)
    assert rec["recommend"] == "under" and 0 <= rec["p_over"] <= 1


# ---------- 3. PER-GAME pipeline: regular (strategy OFF) ----------
def test_matchday_regular_pipeline(monkeypatch):
    led, rled, cap = _isolate(monkeypatch)
    m = {"match_id": 1, "home": "Norway", "away": "France", "stage": "Group",
         "detonator": True, "odds": {"H": 4.2, "D": 3.6, "A": 1.85}}
    res = process_match(m, "T-7m", build_card=_build)        # no strategy
    assert res["status"] == "ok" and res["delivered"]
    assert cap and cap[-1]["pick_direction"] in ("H", "D", "A")
    assert "strategy" not in cap[-1]                          # pure EV
    assert rled.summary()["ok"] == 1


# ---------- 3b. PER-GAME pipeline: strategy ON (behind, late) ----------
def test_matchday_strategy_on(monkeypatch):
    led, rled, cap = _isolate(monkeypatch)
    m = {"match_id": 2, "home": "Norway", "away": "France", "stage": "Group",
         "odds": {"H": 4.2, "D": 3.6, "A": 1.85}}
    ctx = {"your_points": 0, "leader_points": 80, "games_left": 1}
    res = process_match(m, "T-7m", build_card=_build, strategy_context=ctx, strategy_tilt=0.6)
    assert res["status"] == "ok" and res["delivered"]
    # strategy block present (may or may not deviate, but it ran)
    assert "strategy" in cap[-1]


# ---------- 4. HARD CASE: garbage odds → degrade to model-only, still delivers ----------
def test_degrades_on_bad_odds(monkeypatch):
    led, rled, cap = _isolate(monkeypatch)
    m = {"match_id": 3, "home": "A", "away": "B", "stage": "Group",
         "odds": {"H": 0, "D": 0, "A": 0}}          # invalid odds
    res = process_match(m, "T-7m", build_card=_build)
    assert res["status"] == "ok" and res["delivered"]            # no crash, card still out


# ---------- 5. HARD CASE: hard failure is loud + stage-attributed ----------
def test_loud_failure_attributed(monkeypatch):
    led, rled, cap = _isolate(monkeypatch)
    def boom(match):
        with obs.staged("odds"):
            raise ConnectionError("odds 503")
    res = process_match({"match_id": 4, "home": "A", "away": "B"}, "T-7m",
                        build_card=boom, max_attempts=2)
    assert res["status"] == "failed" and res["stage"] == "odds"
    assert any(isinstance(c, tuple) and c[0] == "ALERT" for c in cap)   # alert fired
    assert "[odds]" in rled.summary()["failures"][0]["detail"]
