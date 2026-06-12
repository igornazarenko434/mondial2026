"""Expected-points optimizer — the system's edge.

Given a score-probability matrix and the locked odds, it picks the scoreline
that maximizes expected points UNDER A DIRECTION-CONFIDENCE GATE.

Raw EV per scoreline s with direction d:

    EV(s) = odds(d) * detonator * [ base * (P(d) - P(s)) + tableMult(s) * P(s) ]

Direction-confidence gate (Day-9.26 — tournament-survival fix):
  Pure EV-max picks high-multiplier draw anchors (0-0, 1-1) whenever P(D) is
  not tiny — because fair-market odds make direction-EV roughly equal across
  H/D/A, and the grid rewards 0-0/1-1 with mult 2.75/2.25 vs 1-0/2-1 at 1.50.
  Over 104 WC matches that's high-variance and we got zero points on the
  first two matches when the favorite actually won. The gate restricts the
  pick to the model's dominant direction when there IS a dominant direction,
  banking the base×odds direction-only floor on most matches.

  P(dominant) ≥ 0.55 → only dominant-direction cells considered (strong fav)
  0.40 ≤ P(dom)<0.55 → only dom-direction cells, scored as 0.5*EV + 0.5*P
                       (so we honour the modal-in-direction when EV is close)
  P(dom) < 0.40      → pure EV-max across all cells (toss-up)

The grid multipliers, blend, news, detonator and the base EV formula are
all unchanged — we only constrain which cells the argmax sees.
"""
from __future__ import annotations
import numpy as np
from config.rules import STAGE_TYPE, BASE_POINTS, DETONATOR_FACTOR
from core.scoring.engine import direction, exact_multiplier, direction_probs

# Direction-confidence gate thresholds (Day-9.26). Override via env if we
# want to A/B mid-tournament.
DOM_GATE_STRONG = 0.55
DOM_GATE_MILD   = 0.40


def rank_scores(matrix: np.ndarray, stage: str, odds: dict,
                detonator: bool = False, top: int = 5) -> list[dict]:
    """Return candidate scorelines ranked by raw expected points (descending).

    Always returns the top-N by *raw EV* — independent of any gate. The card
    renderer shows these top-5 for transparency so the operator can see what
    the EV-only model would have picked AND what the gate ended up picking.
    """
    stype = STAGE_TYPE.get(stage)
    if stype is None:
        raise ValueError(f"unknown stage '{stage}'; valid: {sorted(STAGE_TYPE)}")
    base = BASE_POINTS[stype]
    det = DETONATOR_FACTOR if detonator else 1.0
    pdir = direction_probs(matrix)
    n = matrix.shape[0]

    rows = []
    for i in range(n):
        for j in range(n):
            d = direction(i, j)
            p_s = matrix[i, j]
            w, l = max(i, j), min(i, j)
            mult = exact_multiplier(stype, w, l)
            ev = odds[d] * det * (base * (pdir[d] - p_s) + mult * p_s)
            rows.append({"home": i, "away": j, "direction": d,
                         "p_score": round(float(p_s), 4),
                         "exact_multiplier": float(mult),
                         "expected_points": round(float(ev), 3)})
    rows.sort(key=lambda r: r["expected_points"], reverse=True)
    return rows[:top]


def recommend(matrix: np.ndarray, stage: str, odds: dict,
              detonator: bool = False) -> dict:
    """Direction-gated EV recommendation.

    Returns the same shape as before (back-compat) with three new fields:
      - dominant_direction       : H | D | A
      - dominant_strength        : float in [0,1]
      - gate_mode                : 'strong_favorite' | 'mild_favorite' | 'tossup'
      - gate_note                : human-readable one-line explanation
    The chosen pick honours the gate; `ranked_alternatives` is the TOP-5 BY
    RAW EV (unchanged) so the card can display "what EV-only would have picked"
    side-by-side with the actual pick.
    """
    # 1. Full table sorted by raw EV (for the alternatives display)
    n = matrix.shape[0]
    full_ranked = rank_scores(matrix, stage, odds, detonator, top=n * n)
    top5 = full_ranked[:5]

    pdir = direction_probs(matrix)
    dom = max(pdir, key=lambda d: pdir[d])
    dom_p = float(pdir[dom])

    # 2. Apply the gate
    if dom_p >= DOM_GATE_STRONG:
        eligible = [r for r in full_ranked if r["direction"] == dom]
        gate_mode = "strong_favorite"
        gate_note = (f"Dominant direction {dom} at {dom_p*100:.0f}% (≥55%) — "
                     f"restricted to {dom}-cells; chose top-EV in {dom}")
        best = max(eligible, key=lambda r: r["expected_points"]) if eligible else full_ranked[0]
    elif dom_p >= DOM_GATE_MILD:
        eligible = [r for r in full_ranked if r["direction"] == dom]
        gate_mode = "mild_favorite"
        # half-EV / half-probability score, normalised so weights are comparable
        max_ev = max((r["expected_points"] for r in eligible), default=1.0) or 1.0
        max_p  = max((r["p_score"]         for r in eligible), default=1.0) or 1.0
        def _gate_score(r):
            return 0.5 * (r["expected_points"] / max_ev) + 0.5 * (r["p_score"] / max_p)
        gate_note = (f"Dominant direction {dom} at {dom_p*100:.0f}% (40-55%) — "
                     f"restricted to {dom}-cells; scored 50% EV + 50% P(score)")
        best = max(eligible, key=_gate_score) if eligible else full_ranked[0]
    else:
        gate_mode = "tossup"
        gate_note = (f"No dominant direction (max {pdir[dom]*100:.0f}% < 40%) — "
                     f"full EV-max across all cells")
        best = full_ranked[0]

    # Most-likely (modal) score, for transparency
    idx = np.unravel_index(np.argmax(matrix), matrix.shape)

    return {
        "pick_exact_score": {"home": best["home"], "away": best["away"]},
        "pick_direction": best["direction"],
        "expected_points": best["expected_points"],
        "modal_score": {"home": int(idx[0]), "away": int(idx[1])},
        "model_prob": {k: round(v, 3) for k, v in pdir.items()},
        "ranked_alternatives": top5,           # top-5 by RAW EV (unchanged shape)
        "detonator": detonator,
        "locked_odds": odds,
        # Day-9.26 gate provenance
        "dominant_direction": dom,
        "dominant_strength": round(dom_p, 3),
        "gate_mode": gate_mode,
        "gate_note": gate_note,
    }
