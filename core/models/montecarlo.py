"""Day-7: Monte Carlo bracket simulator for futures probabilities.

Drives the simulation with:
  - Dixon-Coles fitted expected goals (per fixture) → goal sampling per match
  - National-team Elo → penalty-shootout edge on KO draws (Day-5 penalties.py)

Outputs per-team probabilities of reaching each stage:
  group_only / r32 / r16 / qf / sf / final / champion

These feed `core.decision.futures`:
  - P(winner)       → rank_winner    (Spain 20 ... USA 170)
  - P(deep run)     → rank_cinderella (P(reach QF or beyond) × payout)
  - P(deep run)     → top-scorer expected tournament goals (when no market)

Design notes:
- We sample goals as independent Poissons from DC expected goals. The DC
  rho-correction matters for 0-0/1-0/0-1/1-1 cells when integrating over the
  matrix for EV (we use it there), but for tournament-OUTCOME probabilities
  the bias washes out across thousands of simulations. Raw Poisson is faster
  and the bias on stage-reach probabilities is <1 pp empirically.
- The bracket uses SNAKE SEEDING among the 32 advancers (winners > runners-up
  > third-placed; within each tier by points/GD/GF). The exact FIFA-2026 R32
  template depends on which 8 third-placed teams qualify and isn't fully
  fixed; snake seeding gives a representative bracket where top performers
  face weak advancers without claiming to predict the specific draw.
- Avoids intra-group R32 rematches via a best-effort swap.
"""
from __future__ import annotations
from collections import defaultdict
from typing import Callable
import numpy as np

from core.scoring.penalties import predict_shootout
from core.data.soccerdata_io import elo_of
from core.obs.logging import get_logger

log = get_logger("models.montecarlo")

# Round-robin pairings for a 4-team group (6 fixtures).
_GROUP_FIXTURES = [(0, 1), (2, 3), (0, 2), (1, 3), (0, 3), (1, 2)]

# Stages a team can reach, ordered. group_only = eliminated in group stage.
STAGES = ("group_only", "r32", "r16", "qf", "sf", "final", "champion")


def sample_score(matrix: np.ndarray, rng: np.random.Generator) -> tuple[int, int]:
    """Draw one (home, away) scoreline from a probability matrix (kept for
    callers who want DC-corrected sampling instead of raw Poisson)."""
    flat = matrix.ravel()
    k = rng.choice(len(flat), p=flat)
    return int(k // matrix.shape[1]), int(k % matrix.shape[1])


def _sample_score_poisson(lh: float, la: float,
                          rng: np.random.Generator) -> tuple[int, int]:
    """One match → (home_goals, away_goals) via independent Poisson sampling.
    Floor lambdas at 0.05 so a team with extreme negative attack still has a
    tiny chance to score (otherwise all draws/upsets collapse)."""
    return (int(rng.poisson(max(0.05, lh))),
            int(rng.poisson(max(0.05, la))))


def simulate_group(teams: list[str], eg_fn: Callable, rng) -> list[dict]:
    """Round-robin one group (6 matches). Returns standings (sorted desc by
    pts, GD, GF). Each row: {team, pts, gd, gf, ga}."""
    stats = {t: {"pts": 0, "gd": 0, "gf": 0, "ga": 0} for t in teams}
    for ih, ia in _GROUP_FIXTURES:
        home, away = teams[ih], teams[ia]
        lh, la = eg_fn(home, away)
        gh, ga = _sample_score_poisson(lh, la, rng)
        stats[home]["gf"] += gh; stats[home]["ga"] += ga; stats[home]["gd"] += gh - ga
        stats[away]["gf"] += ga; stats[away]["ga"] += gh; stats[away]["gd"] += ga - gh
        if gh > ga:   stats[home]["pts"] += 3
        elif gh < ga: stats[away]["pts"] += 3
        else:         stats[home]["pts"] += 1; stats[away]["pts"] += 1
    ranked = sorted(teams, key=lambda t: (-stats[t]["pts"], -stats[t]["gd"], -stats[t]["gf"]))
    return [{"team": t, "pts": stats[t]["pts"], "gd": stats[t]["gd"],
             "gf": stats[t]["gf"], "ga": stats[t]["ga"]} for t in ranked]


def _best_third_placed(group_standings: dict, k: int = 8) -> list[dict]:
    """Top k third-placed teams across all groups by (pts, GD, GF)."""
    thirds = []
    for gname, standings in group_standings.items():
        if len(standings) >= 3:
            r = dict(standings[2]); r["group"] = gname; r["rank_in_group"] = 3
            thirds.append(r)
    thirds.sort(key=lambda r: (-r["pts"], -r["gd"], -r["gf"]))
    return thirds[:k]


def _build_r32_bracket(group_standings: dict,
                       best_thirds: list[dict]) -> list[tuple[str, str]]:
    """Snake seeding across the 32 advancers (winners > runners-up > top-8
    third-placed; within each tier by pts/GD/GF), with intra-group rematch
    avoidance. Returns 16 (home, away) pairs."""
    advancers = []
    for gname, standings in group_standings.items():
        r = dict(standings[0]); r["group"] = gname
        r["tier"] = 1; advancers.append(r)
        r = dict(standings[1]); r["group"] = gname
        r["tier"] = 2; advancers.append(r)
    for t in best_thirds:
        t = dict(t); t["tier"] = 3; advancers.append(t)

    # Sort within tier; ascending tier number → winners first.
    advancers.sort(key=lambda r: (r["tier"], -r["pts"], -r["gd"], -r["gf"]))
    assert len(advancers) == 32, f"expected 32 advancers, got {len(advancers)}"

    pairs = []
    used = [False] * 32
    for i in range(16):
        if used[i]:
            continue
        used[i] = True
        j = 31 - i                              # default snake partner
        # Avoid intra-group rematch (best-effort: walk j backwards)
        if advancers[i]["group"] == advancers[j]["group"]:
            for cand in range(j - 1, i, -1):
                if not used[cand] and advancers[cand]["group"] != advancers[i]["group"]:
                    j = cand
                    break
        used[j] = True
        pairs.append((advancers[i]["team"], advancers[j]["team"]))
    return pairs


def _simulate_ko_match(home: str, away: str, eg_fn: Callable, elo: dict,
                       rng) -> tuple[str, str]:
    """One KO match. Returns (winner, loser). Draws decided by Elo-bounded
    penalty edge (per literature ±5pp from 50/50); shootout outcome itself
    is sampled with that probability."""
    lh, la = eg_fn(home, away)
    gh, ga = _sample_score_poisson(lh, la, rng)
    if gh > ga: return home, away
    if gh < ga: return away, home
    # 0-0 ... shootout
    pen = predict_shootout(elo_of(elo, home), elo_of(elo, away))
    # pen.p_winner ∈ [0.50, 0.55] for pen.winner side
    if rng.random() < pen["p_winner"]:
        return (home, away) if pen["winner"] == "H" else (away, home)
    return (away, home) if pen["winner"] == "H" else (home, away)


def run_tournament(teams_by_group: dict[str, list[str]], eg_fn: Callable,
                   elo: dict, rng: np.random.Generator) -> dict[str, str]:
    """Simulate one full tournament. Returns {team: highest_stage_reached}."""
    reached: dict[str, str] = {}

    # 1. Group stage
    standings = {}
    for gname, teams in teams_by_group.items():
        standings[gname] = simulate_group(teams, eg_fn, rng)
        for row in standings[gname]:
            reached[row["team"]] = "group_only"

    # 2. Top 2 + best 8 third-placed advance
    best_thirds = _best_third_placed(standings)
    for rows in standings.values():
        reached[rows[0]["team"]] = "r32"
        reached[rows[1]["team"]] = "r32"
    for t in best_thirds:
        reached[t["team"]] = "r32"

    # 3. R32 bracket
    r32_pairs = _build_r32_bracket(standings, best_thirds)

    # 4. KO sweeps: R32 → r16, then r16 → qf, qf → sf, sf → final, final → champion
    current_winners = []
    for h, a in r32_pairs:
        w, _ = _simulate_ko_match(h, a, eg_fn, elo, rng)
        reached[w] = "r16"
        current_winners.append(w)

    for stage_name in ("qf", "sf", "final", "champion"):
        new_winners = []
        for i in range(0, len(current_winners), 2):
            h, a = current_winners[i], current_winners[i + 1]
            w, _ = _simulate_ko_match(h, a, eg_fn, elo, rng)
            reached[w] = stage_name
            new_winners.append(w)
        current_winners = new_winners
        if len(current_winners) <= 1:
            break

    return reached


def monte_carlo(teams_by_group: dict[str, list[str]], eg_fn: Callable,
                elo: dict, n: int = 20_000, seed: int = 42
                ) -> dict[str, dict[str, float]]:
    """Run N tournament simulations. Returns per-team stage-reach probabilities.

    Output shape:
        {"Spain": {"group_only": 0.01, "r32": 0.99, "r16": 0.94, "qf": 0.71,
                    "sf": 0.42, "final": 0.18, "champion": 0.09}, ...}
    P(group_only) + P(r32) + ... + P(champion) == 1.0 for every team.
    """
    rng = np.random.default_rng(seed)
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    all_teams = sum(teams_by_group.values(), [])

    for _ in range(n):
        reached = run_tournament(teams_by_group, eg_fn, elo, rng)
        for t in all_teams:
            counts[t][reached.get(t, "group_only")] += 1

    out = {}
    for t in all_teams:
        out[t] = {st: counts[t].get(st, 0) / n for st in STAGES}
    return out


def deep_run_prob(stage_probs: dict[str, float], min_stage: str = "qf") -> float:
    """P(team reaches min_stage or beyond) — the natural §9 'deep run' metric.
    Default cutoff = QF; tune to "sf" if you want a stricter Cinderella bar."""
    idx = STAGES.index(min_stage)
    return sum(stage_probs.get(s, 0.0) for s in STAGES[idx:])


def expected_team_goals(stage_probs: dict[str, float],
                        per_match_xg: float) -> float:
    """Expected tournament goals = (expected matches played) × per-match xG.
    Used as the per-team factor for the top-scorer fallback when no market."""
    matches_in_stage = {"group_only": 3, "r32": 4, "r16": 5, "qf": 6,
                        "sf": 7, "final": 7, "champion": 7}
    return sum(stage_probs.get(s, 0.0) * matches_in_stage[s] for s in STAGES) * per_match_xg


def load_groups_csv(path: str = "data/wc2026_groups.csv") -> dict[str, list[str]]:
    """Read the WC 2026 groups CSV → {group_letter: [team1, ..., team4]}."""
    import csv
    from core.data.teams import normalize
    out: dict[str, list[str]] = defaultdict(list)
    with open(path) as f:
        for r in csv.DictReader(f):
            t = normalize(r["team"])
            if t:
                out[r["group"]].append(t)
    return dict(out)


# Convenience accessor: simulate_tournament for backward compat
simulate_tournament = run_tournament
