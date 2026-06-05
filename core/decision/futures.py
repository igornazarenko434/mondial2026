"""Pre-tournament 'futures' / wide bets (§7–§10) — locked before 11.06 21:59.

These are scored as fixed points if correct (see config.rules payouts), so the
best-practice pick maximizes **Expected Value = P(outcome) × payout**. This module
turns any probability estimate into an EV-ranked table per market. Probabilities
should come from the sharpest source available — **de-vigged market futures odds**
(`implied_probs`) — or from a Monte-Carlo bracket sim (core/models/montecarlo).

For winning (not just EV), the strategy layer's differentiation note applies most
here (longshots like USA 170 are the highest-leverage picks) — see docs/STRATEGY.md.
"""
from __future__ import annotations
from config.rules import WINNER_PAYOUT, SCORER_PAYOUT, CINDERELLA_PAYOUT


def implied_probs(decimal_odds: dict) -> dict:
    """Outright (many-runner) decimal odds → normalized probabilities (removes the
    overround across all runners). Robust to missing/invalid odds."""
    inv = {k: 1.0 / v for k, v in (decimal_odds or {}).items()
           if isinstance(v, (int, float)) and v and v > 1.0}
    total = sum(inv.values())
    return {k: v / total for k, v in inv.items()} if total > 0 else {}


def ev_table(probabilities: dict, payouts: dict) -> list[dict]:
    """Rank options by EV = prob × payout (descending). Options not in
    `probabilities` get prob 0 (EV 0) so they sort to the bottom, never error."""
    rows = [{"option": opt, "prob": round(probabilities.get(opt, 0.0), 4),
             "payout": pay, "ev": round(probabilities.get(opt, 0.0) * pay, 3)}
            for opt, pay in payouts.items()]
    rows.sort(key=lambda r: r["ev"], reverse=True)
    return rows


def rank_winner(probs: dict) -> list[dict]:      # §7
    return ev_table(probs, WINNER_PAYOUT)


def rank_scorer(probs: dict) -> list[dict]:      # §8
    return ev_table(probs, SCORER_PAYOUT)


def rank_cinderella(probs: dict) -> list[dict]:  # §9
    return ev_table(probs, CINDERELLA_PAYOUT)


def rank_fighter(deep_run_probs: dict) -> list[dict]:  # §10
    """The 'fighter' is a flat 10 pts if your low-seed finishes highest, so there's
    no payout to multiply — you simply want the eligible team most likely to go
    deep. Rank by P(deep run) descending."""
    rows = [{"option": t, "p_deep": round(p, 4)} for t, p in (deep_run_probs or {}).items()]
    rows.sort(key=lambda r: r["p_deep"], reverse=True)
    return rows


def recommend_futures(probs_by_market: dict) -> dict:
    """One call → EV tables + the top pick for every futures market you have
    probabilities for. probs_by_market keys: winner/scorer/cinderella/fighter."""
    tables = {}
    if "winner" in probs_by_market:
        tables["winner"] = rank_winner(probs_by_market["winner"])
    if "scorer" in probs_by_market:
        tables["scorer"] = rank_scorer(probs_by_market["scorer"])
    if "cinderella" in probs_by_market:
        tables["cinderella"] = rank_cinderella(probs_by_market["cinderella"])
    if "fighter" in probs_by_market:
        tables["fighter"] = rank_fighter(probs_by_market["fighter"])
    picks = {m: tbl[0]["option"] for m, tbl in tables.items() if tbl}
    return {"tables": tables, "picks": picks}
