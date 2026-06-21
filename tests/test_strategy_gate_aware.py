"""Day-9.29: gate-aware strategy candidate pool.

Pinned by these tests:
  - strong_favorite + tilt → strategy picks IN-direction variance (5-0 NOT 1-1).
    This is the primary regression scenario: under the pre-fix design, raw-EV
    top-1 was the high-multiplier draw (e.g., 1-1 for Brazil-Haiti) and
    strategize returned it because `ranked[0]` was the draw. The fix gives
    strategy a GATE-AWARE pool where strong_favorite cells are pure-in-direction.
  - tossup + tilt → backwards-compat with raw-EV pool (weight=0 means smooth
    score == ev_norm for every cell, so the pool equals raw-EV top-5).
  - Audit fields populated: pool_size, pool_directions, base_is_gate_pick,
    overrode_gate, pool_source.
  - Graceful fallback when strategy_candidates missing (older rec dict shape).
"""
from core.decision.strategy import recommend_to_win, strategize


def _rec_brazil_haiti():
    """Synthetic Brazil-Haiti recommendation. P(H)=87%, big D odds.
    Gate picks 4-0 (H), but raw-EV top-1 is 1-1 (D) because draw odds dominate.
    """
    return {
        "pick_exact_score": {"home": 4, "away": 0},
        "pick_direction": "H",
        "expected_points": 1.403,
        "gate_mode": "strong_favorite",
        "dominant_direction": "H",
        # Raw-EV ranking — draws dominate because D odds × P(D) > H odds × P(H)
        "ranked_alternatives": [
            {"home": 1, "away": 1, "direction": "D",
             "p_score": 0.041, "expected_points": 1.568, "exact_multiplier": 2.25},
            {"home": 2, "away": 2, "direction": "D",
             "p_score": 0.025, "expected_points": 1.480, "exact_multiplier": 2.75},
            {"home": 4, "away": 0, "direction": "H",
             "p_score": 0.105, "expected_points": 1.403, "exact_multiplier": 4.5},
            {"home": 5, "away": 0, "direction": "H",
             "p_score": 0.089, "expected_points": 1.338, "exact_multiplier": 4.5},
            {"home": 0, "away": 0, "direction": "D",
             "p_score": 0.017, "expected_points": 1.331, "exact_multiplier": 2.75},
        ],
        # Gate-aware pool — strong_favorite means out-of-H cells got zeroed out.
        # All five in-direction H cells with raw EV ranking.
        "strategy_candidates": [
            {"home": 4, "away": 0, "direction": "H",
             "p_score": 0.105, "expected_points": 1.403, "exact_multiplier": 4.5},
            {"home": 5, "away": 0, "direction": "H",
             "p_score": 0.089, "expected_points": 1.338, "exact_multiplier": 4.5},
            {"home": 3, "away": 0, "direction": "H",
             "p_score": 0.080, "expected_points": 1.280, "exact_multiplier": 3.25},
            {"home": 6, "away": 0, "direction": "H",
             "p_score": 0.045, "expected_points": 1.180, "exact_multiplier": 4.5},
            {"home": 4, "away": 1, "direction": "H",
             "p_score": 0.052, "expected_points": 1.140, "exact_multiplier": 4.5},
        ],
    }


def test_strong_favorite_tilt_picks_in_direction_not_draw():
    """REGRESSION: pre-fix Brazil-Haiti picked 1-1 (D) with tilt on. After
    the gate-aware pool, strong_favorite matches always pick within H."""
    rec = _rec_brazil_haiti()
    ctx = {"your_points": 20.66, "leader_points": 65.45,
           "games_left": 88, "second_points": 64.88}
    out = recommend_to_win(rec, ctx, tilt=0.6)
    assert out["pick_direction"] == "H", \
        f"strong-favorite tilt should stay in H, got {out['pick_direction']}"
    # Audit must record that gate was NOT overridden
    assert out["strategy"]["overrode_gate"] is False
    assert out["strategy"]["applied"] is True


def test_strong_favorite_audit_fields_populated():
    """Pool audit fields land on the card for Honeycomb / SQL queries."""
    rec = _rec_brazil_haiti()
    ctx = {"your_points": 20, "leader_points": 65, "games_left": 88}
    out = recommend_to_win(rec, ctx, tilt=0.6)
    s = out["strategy"]
    assert s["pool_size"] == 5
    assert s["pool_directions"] == ["H", "H", "H", "H", "H"]
    assert s["pool_source"] == "strategy_candidates"
    # base_is_gate_pick: gate pick (4-0) is at index 0 of strategy_candidates
    assert s["base_is_gate_pick"] is True


def test_strong_favorite_high_upside_within_direction():
    """At high pressure, tilt should favor higher-upside H cells (5-0 or 6-0)
    over the gate-pick 4-0. Verifies tilt math still works WITHIN the pool."""
    rec = _rec_brazil_haiti()
    # Massive deficit — pressure should push toward max-upside cell
    ctx = {"your_points": 0, "leader_points": 500, "games_left": 1}
    out = recommend_to_win(rec, ctx, tilt=1.0)
    assert out["pick_direction"] == "H"
    # The chosen cell must be DIFFERENT from the safe gate-pick 4-0 here —
    # high pressure + max tilt should reach for the higher-upside H pick.
    assert out["pick_exact_score"] != {"home": 4, "away": 0}, \
        "high-pressure tilt within H should pick a higher-upside cell"


def test_tossup_backwards_compat():
    """Tossups (smooth weight=0) → strategy_candidates equals raw-EV top-5
    → tilt picks the highest-upside cell among ALL directions (including
    legit high-multiplier draws). Backwards-compatible with the prior design."""
    # Synthetic Mexico-South-Africa-style tossup: H/D/A all in 30-40% range,
    # 0-0 and 1-1 have legitimately top raw EV.
    rec = {
        "pick_exact_score": {"home": 0, "away": 0},   # tossup gate-picks the raw EV winner
        "pick_direction": "D",
        "expected_points": 3.435,
        "gate_mode": "tossup",
        "dominant_direction": "D",
        "ranked_alternatives": [
            {"home": 0, "away": 0, "direction": "D",
             "p_score": 0.090, "expected_points": 3.435, "exact_multiplier": 2.75},
            {"home": 1, "away": 1, "direction": "D",
             "p_score": 0.103, "expected_points": 3.182, "exact_multiplier": 2.25},
            {"home": 2, "away": 0, "direction": "H",
             "p_score": 0.147, "expected_points": 2.501, "exact_multiplier": 2.25},
        ],
        # In a tossup, strategy_candidates == raw-EV top-5 because smooth gate
        # score == ev_norm for every cell at weight=0.
        "strategy_candidates": [
            {"home": 0, "away": 0, "direction": "D",
             "p_score": 0.090, "expected_points": 3.435, "exact_multiplier": 2.75},
            {"home": 1, "away": 1, "direction": "D",
             "p_score": 0.103, "expected_points": 3.182, "exact_multiplier": 2.25},
            {"home": 2, "away": 0, "direction": "H",
             "p_score": 0.147, "expected_points": 2.501, "exact_multiplier": 2.25},
        ],
    }
    ctx = {"your_points": 20, "leader_points": 65, "games_left": 88}
    out = recommend_to_win(rec, ctx, tilt=0.6)
    # In a true tossup the draw IS the EV winner — strategy keeps it.
    assert out["pick_direction"] == "D"
    assert out["strategy"]["overrode_gate"] is False


def test_backwards_compat_missing_strategy_candidates():
    """Older recommend() output (no strategy_candidates field) falls back
    gracefully to ranked_alternatives. Strategy still runs."""
    rec = {
        "pick_exact_score": {"home": 1, "away": 0},
        "pick_direction": "H",
        "expected_points": 2.0,
        "ranked_alternatives": [
            {"home": 1, "away": 0, "direction": "H",
             "p_score": 0.30, "expected_points": 2.0},
            {"home": 2, "away": 1, "direction": "H",
             "p_score": 0.10, "expected_points": 1.8},
        ],
        # No "strategy_candidates" key at all.
    }
    ctx = {"your_points": 0, "leader_points": 50, "games_left": 1}
    out = recommend_to_win(rec, ctx, tilt=0.5)
    # Strategy runs without error and stamps the source as ranked_alternatives
    assert "strategy" in out
    assert out["strategy"]["pool_source"] == "ranked_alternatives"


def test_gate_pick_injected_when_missing_from_pool():
    """If the gate pick somehow isn't in strategy_candidates (e.g., the
    smooth scoring zeroed it for some reason), it's injected at index 0 so
    the no-tilt baseline still returns the gate pick."""
    rec = {
        "pick_exact_score": {"home": 7, "away": 0},   # gate picked an oddball
        "pick_direction": "H",
        "expected_points": 0.5,
        "ranked_alternatives": [],
        "strategy_candidates": [
            # Note: gate pick (7,0) is NOT in this list.
            {"home": 4, "away": 0, "direction": "H",
             "p_score": 0.10, "expected_points": 1.4},
            {"home": 5, "away": 0, "direction": "H",
             "p_score": 0.09, "expected_points": 1.3},
        ],
    }
    ctx = {"your_points": 20, "leader_points": 21, "games_left": 100}  # tiny pressure
    out = recommend_to_win(rec, ctx, tilt=0.6)
    # base_is_gate_pick=True confirms gate (7,0) was prepended at index 0
    assert out["strategy"]["base_is_gate_pick"] is True
    assert out["strategy"]["pool_size"] == 3                # injected + 2 originals


def test_heavy_favorite_group_all_in_direction():
    """SIMULATION of the user's worst-case: 3 group matches all
    strong_favorite. Under the pre-fix design every match picked 1-1; under
    the fix every match stays in-direction. Pinned here so we can never
    regress to draw-picks for entire heavy-favorite groups."""
    matches = [
        # Match 1 — Brazil-style 87% H favorite
        _rec_brazil_haiti(),
        # Match 2 — Argentina-style 75% H favorite (different score grid)
        {
            "pick_exact_score": {"home": 3, "away": 0},
            "pick_direction": "H",
            "expected_points": 1.8,
            "gate_mode": "strong_favorite",
            "dominant_direction": "H",
            "ranked_alternatives": [
                {"home": 1, "away": 1, "direction": "D",
                 "p_score": 0.06, "expected_points": 1.95},
                {"home": 3, "away": 0, "direction": "H",
                 "p_score": 0.13, "expected_points": 1.8},
            ],
            "strategy_candidates": [
                {"home": 3, "away": 0, "direction": "H",
                 "p_score": 0.13, "expected_points": 1.8},
                {"home": 4, "away": 0, "direction": "H",
                 "p_score": 0.10, "expected_points": 1.72},
                {"home": 2, "away": 0, "direction": "H",
                 "p_score": 0.15, "expected_points": 1.65},
            ],
        },
        # Match 3 — Spain-style 70% H favorite
        {
            "pick_exact_score": {"home": 2, "away": 0},
            "pick_direction": "H",
            "expected_points": 1.5,
            "gate_mode": "strong_favorite",
            "dominant_direction": "H",
            "ranked_alternatives": [
                {"home": 0, "away": 0, "direction": "D",
                 "p_score": 0.07, "expected_points": 1.6},
                {"home": 2, "away": 0, "direction": "H",
                 "p_score": 0.15, "expected_points": 1.5},
            ],
            "strategy_candidates": [
                {"home": 2, "away": 0, "direction": "H",
                 "p_score": 0.15, "expected_points": 1.5},
                {"home": 3, "away": 0, "direction": "H",
                 "p_score": 0.10, "expected_points": 1.4},
                {"home": 4, "away": 0, "direction": "H",
                 "p_score": 0.06, "expected_points": 1.3},
            ],
        },
    ]
    ctx = {"your_points": 20.66, "leader_points": 65.45, "games_left": 88}
    picks = [recommend_to_win(m, ctx, tilt=0.6) for m in matches]
    # Every match stays in dominant direction — no catastrophic group of draws.
    for out, m in zip(picks, matches):
        assert out["pick_direction"] == m["dominant_direction"], (
            f"strong_favorite should pick in-direction; got "
            f"{out['pick_direction']} for gate pick {m['pick_exact_score']}")
        assert out["strategy"]["overrode_gate"] is False
