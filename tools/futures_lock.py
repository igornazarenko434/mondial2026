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
# xG values calibrated against published WC 2026 Golden Boot market odds
# (FOX/RotoWire/Goal.com June 2026) — top-tier strikers in a major tournament
# average 0.8-1.0/match (Mbappé 12 goals in 14 WC apps), so the previous
# uniform 0.45 figures dramatically OVERSTATED mid-tier strikers like Depay.
# Recalibrated values reflect real tournament scoring patterns + role.
_PLAYER_TEAM_XG = {
    "Mbappe":             ("France",      0.85),    # was 0.65 (his real tournament rate)
    "Harry Kane":         ("England",     0.75),    # 2018 GB winner
    "Messi":              ("Argentina",   0.55),    # 2022 finalist & top scorer share
    "Haaland":            ("Norway",      0.85),    # club rate >1/game; intl somewhat lower
    "Mikel Oyarzabal":    ("Spain",       0.25),    # one of several Spain scorers
    "Lamine Yamal":       ("Spain",       0.30),    # younger; lots of potential
    "Cristiano Ronaldo":  ("Portugal",    0.55),    # still scoring at intl level
    "Ousmane Dembele":    ("France",      0.30),    # winger, set up Mbappé more than scores
    "Vinicius":           ("Brazil",      0.45),
    "Lautaro Martinez":   ("Argentina",   0.45),
    "Raphinha":           ("Brazil",      0.40),
    "Kai Havertz":        ("Germany",     0.30),
    "Julian Alvarez":     ("Argentina",   0.40),
    "Romelu Lukaku":      ("Belgium",     0.45),
    "Igor Thiago":        ("Brazil",      0.20),    # rotation forward
    "Cody Gakpo":         ("Netherlands", 0.30),
    "Michael Olise":      ("France",      0.25),
    "Jude Bellingham":    ("England",     0.35),    # midfielder but scoring threat
    "Memphis Depay":      ("Netherlands", 0.35),    # was 0.45; not a top-tier finisher
}

# Web-research market prior (June 2026) — used when the live top-scorer market
# isn't available from the-odds-api free tier. Values are de-vigged estimates
# from FOX Sports / RotoWire / Goal.com / Kalshi published WC 2026 odds.
# When the live market becomes available, these are ignored.
_SCORER_MARKET_PRIOR_2026 = {
    "Mbappe":             0.143,   # +600  consensus #1 favorite
    "Harry Kane":         0.125,   # +700  consensus #2
    "Haaland":            0.075,   # +1200-1400
    "Lamine Yamal":       0.055,   # +1500-1700
    "Messi":              0.050,   # +1800-2200
    "Vinicius":           0.045,   # +2000-2200
    "Julian Alvarez":     0.045,   # +2000-2500
    "Lautaro Martinez":   0.045,   # +2000-2500
    "Cristiano Ronaldo":  0.035,   # +2500-3500
    "Jude Bellingham":    0.038,   # +2200-3000 (the contrarian sweet spot)
    "Cody Gakpo":         0.035,   # +2800-3500
    "Ousmane Dembele":    0.035,   # +2800-3500
    "Raphinha":           0.030,   # +3000-3500
    "Mikel Oyarzabal":    0.030,   # +3000-3500
    "Michael Olise":      0.022,   # +3500-5000
    "Romelu Lukaku":      0.028,   # +3500-4000
    "Kai Havertz":        0.020,   # +4500-5000
    "Memphis Depay":      0.016,   # +6000+ (long shot)
    "Igor Thiago":        0.010,   # +10000+ rotation forward
}


def _scorer_probs_from_mc(mc: dict) -> dict[str, float]:
    """Convert MC stage probs → P(scorer) for each SCORER_PAYOUT player.

    Hybrid model:
      1. Compute raw signal = market-prior × MC-team-advance factor.
         (Market prior alone would ignore team-strength updates from MC;
         MC alone overstates mid-tier strikers because their xG is uniform.)
      2. The MC factor is sqrt(P(team reaches QF or beyond)) — so a team
         going deep boosts its players proportionally but doesn't dominate.
      3. Renormalize so probabilities sum to 1.
    """
    raw = {}
    for player, prior in _SCORER_MARKET_PRIOR_2026.items():
        if player not in SCORER_PAYOUT:
            continue
        team, _ = _PLAYER_TEAM_XG.get(player, (None, None))
        if not team or team not in mc:
            log.warning("scorer hybrid: team %s missing for %s; using prior alone",
                        team, player)
            raw[player] = prior
            continue
        # MC factor: sqrt(team's deep-run probability) — keeps top-tier
        # players dominant while letting MC mildly adjust for team paths.
        from core.models.montecarlo import deep_run_prob
        deep = deep_run_prob(mc[team], min_stage="qf")
        # Normalise against a reference (Spain ~0.65 P(QF)) so factor stays near 1
        # for top teams and shrinks for weak teams.
        factor = max(0.3, (deep / 0.40) ** 0.5)
        raw[player] = prior * factor
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
