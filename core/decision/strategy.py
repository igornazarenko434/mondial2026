"""Win-probability strategy layer (sits ON TOP of the EV optimizer).

EV-max maximizes your expected total; this nudges the pick toward the choice that
maximizes P(finishing 1st), given your standing:
  • BEHIND (and time running out) → take more variance (longer-odds / rarer score)
    among the near-EV-optimal candidates — you need points others won't have.
  • AHEAD → protect: prefer the safer, higher-probability pick (hedge toward the
    field) so a single bad result can't be leapfrogged.
  • NEUTRAL / TILT=0 → return the pure-EV pick unchanged (non-breaking default).

It only ever chooses among the TOP_K EV candidates, so it never makes a reckless
pick — just leans within the strong options. Standings come from the runs/scoring
layer; no opponent pick data is required (we use the field's points + your gap).
"""
from __future__ import annotations
from config.strategy import DEFAULT_TILT, TOP_K, SWING


def risk_pressure(your_points: float, leader_points: float, games_left: int,
                  second_points: float | None = None, swing: float = SWING) -> float:
    """∈[-1,1]. >0 = behind (take variance); <0 = ahead (protect); 0 = neutral."""
    if games_left <= 0:
        return 0.0
    capacity = games_left * swing                      # points you can still realistically swing
    if capacity <= 0:
        return 0.0
    if your_points < leader_points:                    # behind
        return min(1.0, (leader_points - your_points) / capacity)
    sp = second_points if second_points is not None else your_points
    lead = max(0.0, your_points - sp)                  # ahead by this much
    return -min(1.0, lead / capacity)


def _upside(c: dict) -> float:
    """Points-if-it-hits proxy: EV / probability. Higher = more variance/upside."""
    return c["expected_points"] / max(c.get("p_score", 1e-6), 1e-6)


def strategize(ranked: list[dict], context: dict | None = None,
               tilt: float | None = None) -> dict:
    """Pick among the top-EV candidates using a position-aware win-equity tilt.

    context: {"your_points","leader_points","games_left","second_points"(optional)}
    """
    tilt = DEFAULT_TILT if tilt is None else tilt
    if not ranked:
        return {}
    base = ranked[0]
    if not context or tilt == 0:
        return base
    pr = risk_pressure(context.get("your_points", 0.0), context.get("leader_points", 0.0),
                       context.get("games_left", 0), context.get("second_points"))
    if pr == 0:
        return base
    cands = ranked[:TOP_K]
    ups = [_upside(c) for c in cands]
    lo, hi = min(ups), max(ups)
    ev_scale = abs(base["expected_points"]) or 1.0

    def norm(u):
        return 0.0 if hi == lo else (u - lo) / (hi - lo)

    best, best_score = base, float("-inf")
    for c in cands:
        adj = pr * tilt * norm(_upside(c)) * ev_scale   # +variance when behind, −when ahead
        score = c["expected_points"] + adj
        if score > best_score:
            best_score, best = score, c
    return best


def recommend_to_win(rec: dict, context: dict | None = None,
                     tilt: float | None = None) -> dict:
    """Apply the strategy layer to an ev_optimizer.recommend() result. Returns a
    copy with the (possibly) re-chosen pick and a note explaining any deviation.

    FALLBACK-SAFE: on any missing data / error it returns the original EV pick
    unchanged — strategy can only refine, never break, a recommendation.
    """
    out = dict(rec)
    try:
        eff_tilt = DEFAULT_TILT if tilt is None else max(0.0, min(1.0, tilt))
        if not context or eff_tilt == 0 or "pick_exact_score" not in rec:
            return out                                   # off / nothing to do
        chosen = strategize(rec.get("ranked_alternatives", []), context, eff_tilt)
        if not chosen or "home" not in chosen:
            return out
        deviated = (chosen.get("home") != rec["pick_exact_score"]["home"]
                    or chosen.get("away") != rec["pick_exact_score"]["away"])
        out["pick_exact_score"] = {"home": chosen["home"], "away": chosen["away"]}
        out["pick_direction"] = chosen["direction"]
        out["expected_points"] = chosen["expected_points"]
        out["strategy"] = {"applied": True, "tilt": eff_tilt,
                           "deviated_from_ev": deviated,
                           "ev_optimal_score": rec["pick_exact_score"]}
        return out
    except Exception:                                    # noqa: BLE001 - never break the card
        return dict(rec)
