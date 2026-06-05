"""Penalty-shootout winner prediction — bounded ±0.05 from 50/50 per the
literature; monotonic; deterministic on ties."""
from __future__ import annotations
import pytest
from core.scoring.penalties import (
    predict_shootout, PENALTY_EDGE_CAP, PENALTY_ELO_SCALE
)


def test_equal_elo_is_exact_coin_flip():
    out = predict_shootout(1800.0, 1800.0)
    assert out["winner"] == "H"        # convention: ties → H
    assert out["p_winner"] == 0.5


def test_small_edge_is_small():
    """100-Elo gap should produce a single-digit-percent shootout edge."""
    out = predict_shootout(1900.0, 1800.0)
    assert out["winner"] == "H"
    assert 0.50 < out["p_winner"] < 0.515


def test_asymmetric_edge_away_side_wins():
    """When the away team is stronger, away wins the shootout, p reflects symmetry."""
    h = predict_shootout(1500.0, 1900.0)
    a = predict_shootout(1900.0, 1500.0)
    assert h["winner"] == "A"
    assert a["winner"] == "H"
    assert h["p_winner"] == a["p_winner"]    # symmetric


def test_cap_holds_even_for_huge_gaps():
    """No edge can exceed the literature-bounded cap of 5pp, no matter how
    large the Elo gap — penalties are dominated by random factors."""
    out = predict_shootout(3000.0, 1000.0)
    assert out["winner"] == "H"
    assert out["p_winner"] <= 0.5 + PENALTY_EDGE_CAP + 1e-9
    # And similarly for the away side
    out2 = predict_shootout(1000.0, 3000.0)
    assert out2["winner"] == "A"
    assert out2["p_winner"] <= 0.5 + PENALTY_EDGE_CAP + 1e-9


def test_monotonic_in_elo_gap():
    """Larger Elo gap → larger edge (within cap)."""
    pairs = [predict_shootout(1800 + i * 50, 1800)["p_winner"] for i in range(6)]
    # strictly non-decreasing (small gaps → strictly increasing; saturates near cap)
    for prev, nxt in zip(pairs, pairs[1:]):
        assert nxt >= prev - 1e-9


def test_shape_is_pinned():
    """Card-consuming code joins on these exact keys; pin the shape."""
    out = predict_shootout(2000.0, 1800.0)
    assert set(out.keys()) == {"winner", "p_winner"}
    assert out["winner"] in ("H", "A")
    assert isinstance(out["p_winner"], float)
    assert 0.5 <= out["p_winner"] <= 0.5 + PENALTY_EDGE_CAP + 1e-9


def test_handles_string_inputs_gracefully():
    """Defensive: callers may pass numpy floats, strings from JSON, etc."""
    out = predict_shootout("1900", "1800")
    assert out["winner"] == "H"
    assert out["p_winner"] > 0.5


def test_real_wc_2026_matchups():
    """Sanity check the values for two real WC 2026 fixtures."""
    # Spain (2155) vs Brazil (1988) — Spain favored; edge bounded
    spain = predict_shootout(2155, 1988)
    assert spain["winner"] == "H" and 0.50 < spain["p_winner"] < 0.521

    # Mexico (1875) vs South Africa (1518) — Mexico much stronger; near cap
    mex = predict_shootout(1875, 1518)
    assert mex["winner"] == "H"
    assert 0.535 < mex["p_winner"] <= 0.55      # close to but under the cap
