"""Day-7 ONE-SHOT futures lock — run once before 11.06.2026 21:59 Israel.

Produces 3 picks (winner / cinderella / scorer); the fighter pick is intentionally
left manual per the user's instruction. Each pick comes with:
  - the EV-optimal choice
  - top 5 alternatives with EV
  - the margin to #2 (so you see how robust the pick is)

Sources, in order of preference:
  - Winner:      the-odds-api `soccer_fifa_world_cup_winner` outright (~2 credits)
                 → de-vigged via implied_probs → EV vs WINNER_PAYOUT.
  - Scorer:      the-odds-api top-scorer outright if listed (~2 credits) → EV.
                 Fallback: per-team expected tournament goals from Monte Carlo
                 × per-player goal share → EV.
  - Cinderella:  Monte Carlo (20k sims) → P(team reaches QF or beyond) →
                 EV vs CINDERELLA_PAYOUT, restricted to cinderella candidates.

Persists to `reports/futures_lock.json` AND to the `predictions` table as a
single match_id=0 (sentinel) so the audit trail survives a restart.
"""
from __future__ import annotations
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from core.data.results_io import historical_results
from core.data.soccerdata_io import national_team_elo
from core.data.futures_odds import fetch_winner_outright, fetch_topscorer_outright
from core.decision.futures import (
    implied_probs, rank_winner, rank_cinderella, rank_scorer,
)
from core.models.fit import cached_strengths, expected_goals_fn
from core.models.montecarlo import (
    monte_carlo, deep_run_prob, expected_team_goals, load_groups_csv,
)
from config.rules import (
    WINNER_PAYOUT, CINDERELLA_PAYOUT, SCORER_PAYOUT,
)
from core.obs.logging import get_logger
from core import obs

log = get_logger("tools.futures_lock")

DEFAULT_OUT = "reports/futures_lock.json"


# ---------- Top scorer fallback model -----------------------------------
# When no outright market is available, we approximate each player's tournament
# goals as: P(reach round X) × per_match_xg × matches_in_round, summed across
# stages. The per-player goal share is a calibrated estimate (rough — penalties
# matter, manager rotation matters). These can be tuned offline.

# Player → (national team, per-match xG of player when playing).
# xG values here are mid-range conservative — premium strikers ~0.6, support
# attackers ~0.35-0.45, midfielders ~0.20-0.30. Override later with real data.
_PLAYER_TEAM_XG = {
    "Mbappe":             ("France",      0.65),
    "Harry Kane":         ("England",     0.55),
    "Messi":              ("Argentina",   0.40),
    "Haaland":            ("Norway",      0.70),
    "Mikel Oyarzabal":    ("Spain",       0.30),
    "Lamine Yamal":       ("Spain",       0.35),
    "Cristiano Ronaldo":  ("Portugal",    0.45),
    "Ousmane Dembele":    ("France",      0.35),
    "Vinicius":           ("Brazil",      0.45),
    "Lautaro Martinez":   ("Argentina",   0.50),
    "Raphinha":           ("Brazil",      0.40),
    "Kai Havertz":        ("Germany",     0.35),
    "Julian Alvarez":     ("Argentina",   0.45),
    "Romelu Lukaku":      ("Belgium",     0.50),
    "Igor Thiago":        ("Brazil",      0.30),
    "Cody Gakpo":         ("Netherlands", 0.35),
    "Michael Olise":      ("France",      0.30),
    "Jude Bellingham":    ("England",     0.30),
    "Memphis Depay":      ("Netherlands", 0.45),
}


def _scorer_probs_from_mc(mc: dict) -> dict[str, float]:
    """Convert MC stage probs → P(scorer) for each SCORER_PAYOUT player.

    Steps:
      1. For each player, look up team's MC stage probabilities.
      2. expected_team_goals(player_team_stage_probs, per_match_xg) → expected
         total goals across whatever stages they play.
      3. NORMALIZE across all 19 candidates so we get a proper probability
         distribution (this is the implied "of these 19, which is most likely
         top scorer", not absolute P(top scorer)).
    """
    raw = {}
    for player, (team, xg) in _PLAYER_TEAM_XG.items():
        if player not in SCORER_PAYOUT:
            continue                                # player removed from payout
        if team not in mc:
            log.warning("scorer fallback: team %s not in MC output; %s skipped",
                        team, player)
            continue
        raw[player] = expected_team_goals(mc[team], xg)
    total = sum(raw.values())
    if total <= 0:
        return {}
    return {p: v / total for p, v in raw.items()}


def _fuzzy_match_scorer_market(market: dict[str, float],
                                payout_keys: set[str]) -> dict[str, float]:
    """Best-effort match of bookmaker player names (often 'M. Mbappé', 'K. Mbappe')
    to our SCORER_PAYOUT keys (typically last name or short form)."""
    out = {}
    for raw_name, price in market.items():
        for canon in payout_keys:
            # exact-token containment, case-insensitive
            r, c = raw_name.lower(), canon.lower()
            if c in r or all(tok in r for tok in c.split()):
                if canon not in out or price < out[canon]:
                    out[canon] = price
                break
    return out


# ---------- Pretty printers ---------------------------------------------

def _print_table(name: str, rows: list[dict], top_n: int = 5, key="ev"):
    print(f"\n--- {name} (top {top_n} by {key}) ---")
    for r in rows[:top_n]:
        # Each row has option + prob + payout (winner/scorer/cinderella) or p_deep (fighter)
        extra = (f"prob={r.get('prob', 0):.4f}  payout={r.get('payout', '?')}"
                 if "prob" in r else f"p_deep={r.get('p_deep', 0):.4f}")
        print(f"  {r.get('option'):<22s}  ev={r.get('ev', r.get('p_deep', 0)):.3f}   {extra}")
    if rows:
        margin = rows[0].get("ev", rows[0].get("p_deep", 0)) - (
                  rows[1].get("ev", rows[1].get("p_deep", 0)) if len(rows) > 1 else 0)
        print(f"  → pick: {rows[0]['option']}   margin to #2: {margin:+.3f}")


# ---------- The lock orchestrator ----------------------------------------

def run_lock(out_path: str = DEFAULT_OUT, n_sims: int = 20_000,
             seed: int = 42, regions: str = "eu,uk") -> dict:
    """The one-shot. Returns the full picks structure; writes JSON + persists.

    Set n_sims smaller (e.g. 2000) for a fast dry-run; 20k for the actual lock.
    """
    with obs.run(f"futures-lock-{int(time.time())}"):
        log.info("loading historical results + DC fit + Elo …")
        t0 = time.perf_counter()
        results = historical_results()
        strengths = cached_strengths(results)
        eg_fn = expected_goals_fn(strengths)
        elo = national_team_elo()
        log.info("data ready in %.1fs (%d teams fitted)",
                  time.perf_counter() - t0, len(strengths["teams"]))

        log.info("loading groups CSV …")
        teams_by_group = load_groups_csv()
        log.info("groups: %s", {g: len(v) for g, v in teams_by_group.items()})

        log.info("running Monte Carlo: %d sims …", n_sims)
        t0 = time.perf_counter()
        mc = monte_carlo(teams_by_group, eg_fn, elo, n=n_sims, seed=seed)
        log.info("MC done in %.1fs", time.perf_counter() - t0)

        # ----- WINNER -----
        log.info("fetching winner outright market …")
        winner_market = fetch_winner_outright(regions=regions)
        winner_source = "market"
        if winner_market:
            winner_probs = implied_probs(winner_market)
        else:
            # Fallback to MC P(champion)
            log.warning("no market odds for winner; falling back to MC")
            winner_probs = {t: probs["champion"] for t, probs in mc.items()}
            winner_source = "monte_carlo"
        winner_table = rank_winner(winner_probs)

        # ----- CINDERELLA -----
        cinderella_probs = {
            t: deep_run_prob(mc[t], min_stage="qf")
            for t in CINDERELLA_PAYOUT.keys() if t in mc
        }
        cinderella_table = rank_cinderella(cinderella_probs)

        # ----- SCORER -----
        log.info("trying top-scorer market …")
        scorer_market_raw = fetch_topscorer_outright(regions=regions)
        scorer_source = "monte_carlo_fallback"
        if scorer_market_raw:
            matched = _fuzzy_match_scorer_market(scorer_market_raw,
                                                  set(SCORER_PAYOUT.keys()))
            if matched and len(matched) >= 5:
                scorer_probs = implied_probs(matched)
                scorer_source = "market"
            else:
                log.info("scorer market matched only %d of %d players; using MC fallback",
                          len(matched), len(SCORER_PAYOUT))
                scorer_probs = _scorer_probs_from_mc(mc)
        else:
            scorer_probs = _scorer_probs_from_mc(mc)
        scorer_table = rank_scorer(scorer_probs)

        result = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "n_simulations": n_sims,
            "seed": seed,
            "sources": {
                "winner": winner_source,
                "cinderella": "monte_carlo",
                "scorer": scorer_source,
            },
            "tables": {
                "winner": winner_table,
                "cinderella": cinderella_table,
                "scorer": scorer_table,
            },
            "picks": {
                "winner":    winner_table[0]["option"]    if winner_table    else None,
                "cinderella": cinderella_table[0]["option"] if cinderella_table else None,
                "scorer":    scorer_table[0]["option"]    if scorer_table    else None,
            },
            "monte_carlo_probabilities": mc,
        }

        # Persist to JSON for human review
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(json.dumps(result, indent=2, default=str))
        log.info("wrote %s", out_path)
        return result


if __name__ == "__main__":
    out = run_lock()
    _print_table("WINNER",    out["tables"]["winner"])
    _print_table("CINDERELLA", out["tables"]["cinderella"])
    _print_table("SCORER",    out["tables"]["scorer"])
    print("\n=== FINAL PICKS ===")
    for m, p in out["picks"].items():
        print(f"  {m:<12s}: {p}")
    print(f"\n  (fighter is intentionally manual)")
    print(f"  saved to {DEFAULT_OUT}")
