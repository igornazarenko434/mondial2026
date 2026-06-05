"""Penalty-shootout winner prediction for knockout draws.

In knockout matches the model often gives a non-trivial draw probability; if
the game ends level after 90' + extra time, it's decided on penalties — and
THAT side of the result determines who advances (and, per the rules' §15c-e
and §16c-d, who gets shootout partial credit). For the model+card output we
need a probability that the higher-Elo side wins the shootout.

The football literature is consistent: penalty shootouts are dominated by
random factors (single kick outcomes, goalkeeper guess timing, kicker
psychology). The Elo edge in a shootout is therefore SMALL — empirical
studies cap it at roughly ±5 percentage points around 50/50 even for the
biggest international Elo gaps. We mirror that:

    P(home wins shootout) = 0.50 + cap * tanh((elo_h - elo_a) / scale)

with cap = 0.05 and scale = 400 (Elo's standard "one expected-goal step")
so that:
  - equal Elo → 0.500 (true coin flip)
  - 100-pt edge → ~0.512
  - 400-pt edge → ~0.538
  - infinite edge → asymptotic to ±0.55 (the bounded cap)

`predict_shootout` is a pure function — no I/O, deterministic, no external
deps. It's invoked from `predict.match_card` only when stage != Group AND
draw_prob >= some threshold (default 0.15 per the Day-6 spec).
"""
from __future__ import annotations
from math import tanh

PENALTY_EDGE_CAP = 0.05      # max ±pp from 50/50 (literature-bounded)
PENALTY_ELO_SCALE = 400.0    # Elo scale (one standard "expected goal" step)


def predict_shootout(elo_home: float, elo_away: float) -> dict:
    """Predict the penalty-shootout winner given each side's Elo.

    Returns {"winner": "H"|"A", "p_winner": float in [0.50, 0.55]}.
    Always returns "winner" deterministically: equal Elo → "H" by convention
    (with p_winner = 0.50) so a caller can join unconditionally on either key.
    """
    delta = (float(elo_home) - float(elo_away)) / PENALTY_ELO_SCALE
    edge = PENALTY_EDGE_CAP * tanh(delta)
    if edge >= 0:
        return {"winner": "H", "p_winner": round(0.5 + edge, 3)}
    return {"winner": "A", "p_winner": round(0.5 - edge, 3)}
