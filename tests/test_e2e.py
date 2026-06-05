"""Full pipeline end-to-end (no network): calendar -> scheduler -> pipeline ->
model -> EV -> delivery, plus observability metrics and failure attribution.
"""
import sqlite3
from datetime import datetime, timezone, timedelta
import numpy as np

from store import repo
from schedule.runner import SchedulerDaemon
from core import obs
from core.obs.cost import CostLedger
from core.obs.runs import RunLedger
from core.models.blend import blended_matrix
from core.models.elo import outcome_probs, expected_goals_from_elo
from core.data.oddsapi import devig
from core.decision.ev_optimizer import recommend


def _db_with_match(mins_to_ko=7):
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE matches (match_id INTEGER PRIMARY KEY, utc_kickoff TEXT,
        local_kickoff TEXT, stage TEXT, grp TEXT, home TEXT, away TEXT, status TEXT,
        home_goals INTEGER, away_goals INTEGER, detonator INTEGER DEFAULT 0)""")
    ko = (datetime.now(timezone.utc) + timedelta(minutes=mins_to_ko)).isoformat()
    conn.execute("INSERT INTO matches VALUES (401,?,?,?,?,?,?,?,?,?,?)",
                 (ko, ko, "Group", "I", "Norway", "France", "TIMED", None, None, 1))
    conn.commit()
    return conn


def _real_build(match):
    """A realistic staged build_card (no network) exercising the degradation-aware
    stages: odds -> model -> decision."""
    with obs.staged("odds"):
        odds = {"H": 4.20, "D": 3.60, "A": 1.85}
        market = devig(odds)
    with obs.staged("model"):
        elo_p = outcome_probs(1840, 2050)
        eh, ea = expected_goals_from_elo(1840, 2050)
        matrix = blended_matrix(eh, ea, elo_p, market)
    with obs.staged("decision"):
        rec = recommend(matrix, match.get("stage", "Group"), odds,
                        detonator=bool(match.get("detonator")))
    rec.update({"home": match["home"], "away": match["away"], "stage": match.get("stage")})
    return rec


def test_e2e_success_delivers_and_records(monkeypatch):
    # isolate ledgers to in-memory and capture delivery
    led = CostLedger(":memory:"); rled = RunLedger(":memory:")
    monkeypatch.setattr("core.obs.cost._LEDGER", led, raising=False)
    monkeypatch.setattr("core.obs.runs._LEDGER", rled, raising=False)
    monkeypatch.setattr("core.obs.cost.ledger", lambda: led)
    monkeypatch.setattr("core.obs.runs.runs", lambda: rled)
    delivered = []
    import core.delivery as d
    monkeypatch.setattr(d, "deliver_card", lambda c: delivered.append(c) or True)
    monkeypatch.setattr(d, "alert", lambda t, b: True)

    conn = _db_with_match(7)
    daemon = SchedulerDaemon(lambda: repo.upcoming_matches(conn), _real_build, max_workers=2)
    daemon.tick()
    daemon.pool.shutdown(wait=True)

    # 1) a card was produced and delivered, with the right shape
    assert delivered, "no card delivered"
    card = delivered[-1]
    assert card["home"] == "Norway" and card["pick_direction"] in ("H", "D", "A")
    assert "expected_points" in card and "pick_exact_score" in card
    # 2) run recorded ok + delivered
    summ = rled.summary()
    assert summ["ok"] >= 1 and summ["cards_delivered"] >= 1 and summ["failed"] == 0


def test_e2e_failure_is_attributed_to_stage(monkeypatch):
    led = CostLedger(":memory:"); rled = RunLedger(":memory:")
    monkeypatch.setattr("core.obs.cost._LEDGER", led, raising=False)
    monkeypatch.setattr("core.obs.runs._LEDGER", rled, raising=False)
    alerts = []
    import core.delivery as d
    monkeypatch.setattr(d, "deliver_card", lambda c: True)
    monkeypatch.setattr(d, "alert", lambda t, b: alerts.append((t, b)) or True)

    def failing_build(match):
        with obs.staged("odds"):
            raise ConnectionError("odds provider 503")

    from orchestrator.pipeline import process_match
    res = process_match({"match_id": 1, "home": "A", "away": "B"}, "T-7m",
                        build_card=failing_build, max_attempts=2)
    assert res["status"] == "failed"
    assert res["stage"] == "odds"                       # attributed to the right stage
    # the failure row + alert both name the stage
    failures = rled.summary()["failures"]
    assert failures and "[odds]" in failures[0]["detail"]
    assert alerts and "odds" in alerts[0][1]
