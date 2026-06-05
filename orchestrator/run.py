"""End-to-end demo: produce a recommendation card for one match.

This wires the pieces together WITHOUT live data so you can see the engine work
today. Replace the hard-coded inputs with: Elo from soccerdata_io, expected
goals from dixon_coles.fit_strengths, market probs from oddsapi.consensus_probs,
and locked odds from oddsapi.fetch_match_odds at T-7m.

Run:  python -m orchestrator.run
"""
from __future__ import annotations
import json
from core.models.elo import outcome_probs, expected_goals_from_elo
from core.models.blend import blended_matrix
from core.data.oddsapi import devig
from core.decision.ev_optimizer import recommend


def demo_card():
    # --- inputs that will become live data ---
    home, away, stage, detonator = "Norway", "France", "Group", True
    home_elo, away_elo = 1840, 2050                  # TODO: soccerdata_io.national_team_elo()
    locked_odds = {"H": 4.20, "D": 3.60, "A": 1.85}  # TODO: oddsapi.fetch_match_odds() @ T-7m

    # --- model ---
    elo_p = outcome_probs(home_elo, away_elo)
    market_p = devig(locked_odds)
    eh, ea = expected_goals_from_elo(home_elo, away_elo)   # DC fit replaces this
    matrix = blended_matrix(eh, ea, elo_p, market_p)

    # --- decision ---
    rec = recommend(matrix, stage, locked_odds, detonator=detonator)
    rec.update({"home": home, "away": away, "stage": stage})
    return rec


if __name__ == "__main__":
    from core import obs
    from orchestrator.pipeline import process_match, daily_summary
    obs.setup()
    # run the FULL pipeline (run-status + retry + delivery + loud failure),
    # using the demo card builder so it works with no API keys.
    match = {"match_id": 401, "home": "Norway", "away": "France",
             "stage": "Group", "detonator": True}
    result = process_match(match, window="T-7m", build_card=lambda m: demo_card())
    print("\npipeline result:", result["status"], "| delivered:", result.get("delivered"))
    print("\nrun summary:", daily_summary())
