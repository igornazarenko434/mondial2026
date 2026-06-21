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

    Day-9.29: operates on the GATE-AWARE candidate pool
    `rec["strategy_candidates"]` (top-5 by smooth gate score). At strong
    favorite this pool is pure-in-direction → tilt picks within-direction
    variance (5-0 instead of 4-0) and preserves the direction-only floor.
    Falls back to `ranked_alternatives` (raw EV) for old card dicts.
    """
    out = dict(rec)
    try:
        eff_tilt = DEFAULT_TILT if tilt is None else max(0.0, min(1.0, tilt))
        if not context or eff_tilt == 0 or "pick_exact_score" not in rec:
            return out                                   # off / nothing to do

        # Prefer the gate-aware candidate pool; fall back to raw-EV top-5
        # for backwards compatibility with older recommend() output.
        candidates = (rec.get("strategy_candidates")
                      or rec.get("ranked_alternatives", []))

        # Anchor the strategy baseline to the GATE pick (not raw-EV top-1).
        # If the gate pick isn't already in the candidate pool, inject it at
        # index 0 so `strategize` returns it for low pressure / tilt=0 (the
        # default-when-no-deviation-warranted behavior).
        gate_h = rec["pick_exact_score"]["home"]
        gate_a = rec["pick_exact_score"]["away"]
        gate_in_pool = any(c.get("home") == gate_h and c.get("away") == gate_a
                            for c in candidates)
        if not gate_in_pool:
            gate_cell = {
                "home": gate_h,
                "away": gate_a,
                "direction": rec["pick_direction"],
                "expected_points": rec["expected_points"],
                # p_score / exact_multiplier may be absent on the rec dict but
                # are required by _upside; pull from the matching raw-EV cell
                # if available, else use safe defaults.
                "p_score": next(
                    (c.get("p_score", 1e-6)
                     for c in rec.get("ranked_alternatives", [])
                     if c.get("home") == gate_h and c.get("away") == gate_a),
                    1e-6),
                "exact_multiplier": next(
                    (c.get("exact_multiplier", 0.0)
                     for c in rec.get("ranked_alternatives", [])
                     if c.get("home") == gate_h and c.get("away") == gate_a),
                    0.0),
            }
            candidates = [gate_cell] + list(candidates)

        chosen = strategize(candidates, context, eff_tilt)
        if not chosen or "home" not in chosen:
            return out
        deviated = (chosen.get("home") != rec["pick_exact_score"]["home"]
                    or chosen.get("away") != rec["pick_exact_score"]["away"])
        out["pick_exact_score"] = {"home": chosen["home"], "away": chosen["away"]}
        out["pick_direction"] = chosen["direction"]
        out["expected_points"] = chosen["expected_points"]
        # Day-9.29 audit: first-class visibility into the strategy's choice.
        out["strategy"] = {
            "applied": True,
            "tilt": eff_tilt,
            "deviated_from_ev": deviated,
            "ev_optimal_score": rec["pick_exact_score"],
            # Pool composition
            "pool_size": len(candidates),
            "pool_directions": [c.get("direction") for c in candidates],
            "pool_source": ("strategy_candidates"
                            if rec.get("strategy_candidates")
                            else "ranked_alternatives"),
            # Did the strategy start from the gate pick at index 0?
            "base_is_gate_pick": (candidates[0].get("home") == gate_h
                                   and candidates[0].get("away") == gate_a)
                                  if candidates else False,
            # Did the final chosen cell change DIRECTION vs the gate?
            "overrode_gate": chosen.get("direction") != rec["pick_direction"],
        }
        return out
    except Exception:                                    # noqa: BLE001 - never break the card
        return dict(rec)
