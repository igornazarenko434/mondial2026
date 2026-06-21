"""Expected-points optimizer — the system's edge.

Given a score-probability matrix and the locked odds, it picks the scoreline
that maximizes expected points UNDER A DIRECTION-CONFIDENCE GATE.

Raw EV per scoreline s with direction d:

    EV(s) = odds(d) * detonator * [ base * (P(d) - P(s)) + tableMult(s) * P(s) ]

Direction-confidence gate (Day-9.26 + 9.26.2 smoothing):
  Pure EV-max picks high-multiplier draw anchors (0-0, 1-1) whenever P(D) is
  not tiny — because fair-market odds make direction-EV roughly equal across
  H/D/A, and the grid rewards 0-0/1-1 with mult 2.75/2.25 vs 1-0/2-1 at 1.50.
  Over 104 WC matches that's high-variance and we got zero points on the
  first two matches when the favorite actually won. The gate restricts the
  pick to the model's dominant direction when there IS a dominant direction,
  banking the base×odds direction-only floor on most matches.

  Day-9.26.2 SMOOTH BOUNDARY (no cliff at 0.40 / 0.55):
    weight_to_dom = clip((P(dom) - mild_lower) / (strong - mild_lower), 0, 1)
    score(c) =
      if cell c is in dom direction:
        (1-alpha)*EV_norm + alpha*P_norm     # alpha peaks at mild zone
      else:
        EV_norm * (1 - weight_to_dom)        # penalty grows linearly
    where alpha = 2*weight*(1-weight)  (peaks at 0.5 at mid-range)

  At weight=1 (strong-fav)  → pure EV-max within dom direction only.
  At weight=0 (tossup)      → pure EV-max across ALL cells.
  In between                → smoothly blends; out-of-dom cells get a
                              gradual penalty rather than a hard cutoff.

  Thresholds come from config.rules.GATE_THRESHOLDS — KO/Final/Detonator
  matches activate protection EARLIER because their stakes are higher.

  Tied-direction guardrail: when top-two direction probs are within
  GATE_TIE_MARGIN (default 0.02), force tossup mode regardless of dom_p.
  Prevents arbitrary insertion-order tie-breaks from determining picks.

The grid multipliers, blend, news, detonator and the base EV formula are
all unchanged — we only smoothly weight which cells the argmax considers.
"""
from __future__ import annotations
import numpy as np
from config.rules import (STAGE_TYPE, BASE_POINTS, DETONATOR_FACTOR,
                          GATE_THRESHOLDS, GATE_TIE_MARGIN)
from core.scoring.engine import direction, exact_multiplier, direction_probs


def _thresholds_for(stage_type: str, detonator: bool) -> dict:
    """Return the {strong, mild_lower} pair for this match's risk profile.

    A detonator-flagged match uses the more aggressive 'detonator' thresholds
    REGARDLESS of stage (high-stakes matches deserve earlier protection).
    Otherwise we pick the per-stage default from config.rules.GATE_THRESHOLDS.
    """
    if detonator:
        return GATE_THRESHOLDS["detonator"]
    return GATE_THRESHOLDS.get(stage_type, GATE_THRESHOLDS["group"])


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
    """Smooth direction-gated EV recommendation (Day-9.26.2).

    No more hard 0.40/0.55 cliff. Single smooth scoring function that
    interpolates between three behaviors based on dom_p:
      - dom_p ≤ mild_lower    : pure EV-max across all cells (tossup)
      - dom_p ≥ strong        : EV-max restricted to dom-direction cells
      - mild_lower < dom_p < strong : smoothly blended; out-of-dom cells get
                                       a linear penalty; within-dom cells get
                                       an EV+P_norm mix peaking at mid-range.

    Tied-direction guardrail: if top two direction probs are within
    GATE_TIE_MARGIN, force tossup mode regardless of dom_p — prevents the
    pick from depending on arbitrary tie-breaking.

    Returns the same shape as before (back-compat) plus four gate fields:
      - dominant_direction       : H | D | A
      - dominant_strength        : float in [0,1]
      - gate_mode                : 'strong_favorite' | 'mild_favorite' |
                                    'tossup' | 'tossup_tied'
      - gate_note                : human-readable one-line explanation
    """
    # 1. Full table sorted by raw EV (for the alternatives display)
    n = matrix.shape[0]
    full_ranked = rank_scores(matrix, stage, odds, detonator, top=n * n)
    top5 = full_ranked[:5]

    pdir = direction_probs(matrix)

    # 2. Identify dominant direction with tied-direction guardrail
    sorted_dirs = sorted(pdir.items(), key=lambda kv: -kv[1])
    dom, dom_p_raw = sorted_dirs[0]
    second_dir, second_p = sorted_dirs[1]
    dom_p = float(dom_p_raw)
    tied = (dom_p - float(second_p)) < GATE_TIE_MARGIN

    # 3. Pick thresholds based on stage + detonator profile
    stype = STAGE_TYPE.get(stage)
    thr = _thresholds_for(stype, detonator)
    strong = thr["strong"]
    mild_lower = thr["mild_lower"]

    # 4. Smooth weight: 0 at mild_lower or below, 1 at strong or above
    if tied or dom_p <= mild_lower:
        weight = 0.0
    elif dom_p >= strong:
        weight = 1.0
    else:
        weight = (dom_p - mild_lower) / (strong - mild_lower)

    # alpha peaks at 0.5 in the middle of the smooth band (mild zone)
    # alpha = 2*w*(1-w):  w=0 → 0,  w=0.5 → 0.5,  w=1 → 0
    alpha = 2.0 * weight * (1.0 - weight)

    # 5. Score every cell with the SMOOTH gate function
    max_ev = max((r["expected_points"] for r in full_ranked), default=1.0) or 1.0
    max_p  = max((r["p_score"]         for r in full_ranked), default=1.0) or 1.0

    def _smooth_score(r):
        ev_norm = r["expected_points"] / max_ev
        p_norm  = r["p_score"] / max_p
        if r["direction"] == dom:
            # In dominant direction: blend EV-norm and P-norm.
            # At weight=0 or weight=1, alpha=0 → pure EV.
            # At weight=0.5, alpha=0.5 → half EV + half P (preserves original
            # mild-favorite spec at the midpoint of the smooth band).
            return (1.0 - alpha) * ev_norm + alpha * p_norm
        # Out of dominant direction: linear penalty grows with weight.
        # At weight=0: no penalty (tossup — every cell competes on raw EV).
        # At weight=1: full penalty (effectively excluded — strong-fav).
        return ev_norm * (1.0 - weight)

    best = max(full_ranked, key=_smooth_score)

    # 5b. Day-9.29: gate-aware candidate pool for the strategy (tilt) layer.
    # Sort full_ranked by SMOOTH gate score so the strategy operates on the
    # SAME scoring function that selects the main pick — no more "tilt ON
    # silently bypasses the gate and picks the high-multiplier draw".
    #   strong_favorite (weight=1): out-of-dom cells score 0 → pool is
    #     pure-in-direction → tilt picks within-direction variance (5-0
    #     instead of 4-0), preserving the direction-only partial-credit floor.
    #   mild_favorite (0<weight<1): in-dom cells get EV+P blend boost,
    #     out-of-dom cells get linear (1-weight) penalty → mostly in-direction.
    #   tossup (weight=0): every cell scores ev_norm → pool == raw-EV top-5
    #     (backwards-compatible — true tossups still allow draw picks).
    strategy_candidates = sorted(full_ranked, key=_smooth_score, reverse=True)[:5]

    # 6. Label the gate mode for the audit trail
    if tied:
        gate_mode = "tossup_tied"
        gate_note = (
            f"Top two directions tied within {GATE_TIE_MARGIN:.2f} "
            f"({dom} {dom_p*100:.0f}%, {second_dir} {second_p*100:.0f}%) "
            f"— full EV-max across all cells"
        )
    elif weight == 0.0:
        gate_mode = "tossup"
        gate_note = (
            f"Dominant direction {dom} at {dom_p*100:.0f}% ≤ "
            f"{mild_lower*100:.0f}% — full EV-max across all cells"
        )
    elif weight == 1.0:
        gate_mode = "strong_favorite"
        gate_note = (
            f"Dominant direction {dom} at {dom_p*100:.0f}% ≥ "
            f"{strong*100:.0f}% — restricted to {dom}-cells; chose top-EV "
            f"in {dom}"
        )
    else:
        gate_mode = "mild_favorite"
        gate_note = (
            f"Dominant direction {dom} at {dom_p*100:.0f}% "
            f"(smooth band {mild_lower*100:.0f}-{strong*100:.0f}%, "
            f"weight {weight:.2f}, alpha {alpha:.2f}) — "
            f"smooth blend of EV-max + modal-P within {dom}; out-of-{dom} "
            f"cells penalized {(1-weight)*100:.0f}% × raw EV"
        )

    # 7. Most-likely (modal) score, for transparency
    idx = np.unravel_index(np.argmax(matrix), matrix.shape)

    return {
        "pick_exact_score": {"home": best["home"], "away": best["away"]},
        "pick_direction": best["direction"],
        "expected_points": best["expected_points"],
        "modal_score": {"home": int(idx[0]), "away": int(idx[1])},
        "model_prob": {k: round(v, 3) for k, v in pdir.items()},
        "ranked_alternatives": top5,
        "strategy_candidates": strategy_candidates,
        "detonator": detonator,
        "locked_odds": odds,
        # Gate provenance (Day-9.26 + 9.26.2)
        "dominant_direction": dom,
        "dominant_strength": round(dom_p, 3),
        "gate_mode": gate_mode,
        "gate_note": gate_note,
        "gate_weight": round(weight, 3),
        "gate_thresholds": {"strong": strong, "mild_lower": mild_lower},
    }
