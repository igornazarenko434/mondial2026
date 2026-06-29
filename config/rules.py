"""Single source of truth for the friends' Toto Mondial 2026 scoring rules.

Everything that defines how points are earned lives here so the scoring engine,
the EV optimizer and the tests all read the same numbers. Values were verified
against the rules PDF (worked examples + diagonal structure of the tables).

Exact-score tables are indexed by (winner_goals, loser_goals).
For a draw, winner_goals == loser_goals (e.g. 1-1 -> (1, 1)).
"""

# --- Stage -> table type and base "direction-only" points -------------------
STAGE_TYPE = {
    "Group": "group",
    "R32": "ko", "R16": "ko", "QF": "ko",
    "SF": "final", "3rd": "final", "Final": "final",
}
BASE_POINTS = {"group": 1.0, "ko": 1.5, "final": 2.0}      # §12 / §15a / §16a

GROUP_RESET_FACTOR = 0.85                                   # §14  (-15% after groups)

# --- Exact-score multiplier tables -----------------------------------------
# Built from arrays [loser_goals][winner_goals]; None = impossible (winner<loser).
# Row 0 (clean-sheet wins): Day-9.7 fix — was [2.75, 2.25, 3.25, 4.5, ...] but
# Negev's server-side scoring grid (managerTables.grids.groupStage, our source
# of truth for what actually gets awarded) has 1-0=1.5, 2-0=2.25, 3-0=3.25.
# Our previous values came from a misread/transcription of the PDF row, off
# by one column. The pattern Negev uses is internally consistent:
#   1-0 ↔ 2-1 = 1.5  (same difficulty — low-scoring home win)
#   2-0 ↔ 3-1 = 2.25
#   3-0 ↔ 4-1 = 3.25
# tools/negev_consistency_audit.py verifies all 49 cells in 3 grids agree.
_GROUP = [  # §12
    [2.75, 1.5, 2.25, 3.25, 4.5, 4.5, 4.5, 4.5],
    [None, 2.25, 1.5,  3.25, 4.5, 4.5, 4.5, 4.5],
    [None, None, 2.75, 3.25, 4.5, 4.5, 4.5, 4.5],
    [None, None, None, 4.5,  4.5, 4.5, 4.5, 4.5],
]
_KO = [  # §15  (R32 / R16 / QF, result at 120')
    [3.75, 2.25, 3.5,  4.5,  8.25, 8.25, 8.25, 8.25],
    [None, 3.0,  2.25, 4.5,  8.25, 8.25, 8.25, 8.25],
    [None, None, 3.75, 4.5,  8.25, 8.25, 8.25, 8.25],
    [None, None, None, 8.25, 8.25, 8.25, 8.25, 8.25],
]
_FINAL = [  # §16  (SF / 3rd / Final)
    # Day-9.33 (2026-06-29 audit alert): Negev silently re-priced semiAndFinal
    # to a 0.75× schedule for "normal" scorelines (0-4 goals each side); only
    # 5+ goal blowouts retained the legacy ×11 cap. tools/audit_negev_multipliers
    # caught the drift at 06:55 IDT; this patch realigns our payoff math so
    # ev_optimizer + score_match compute against the SAME multipliers Negev
    # will actually award. Pre-Day-9.33 values are recoverable in git history.
    # ROW 4 explicitly enumerated (vs the older 3-row tables) because Negev's
    # SF/Final cap kicks in at winner=5+, not winner=4 — 4-4 draws and 4-X
    # cells pay 8.25 (not the cap 11). _KO and _GROUP don't need row 4 because
    # their cap value EQUALS the row-4 value (no semantic difference).
    [3.75, 2.25, 3.5,  4.5,  8.25, 11, 11, 11],
    [None, 3,    2.25, 4.5,  8.25, 11, 11, 11],
    [None, None, 3.75, 4.5,  8.25, 11, 11, 11],
    [None, None, None, 8.25, 8.25, 11, 11, 11],
    [None, None, None, None, 8.25, 11, 11, 11],
]

def _to_dict(arr):
    d = {}
    for loser, row in enumerate(arr):
        for winner, val in enumerate(row):
            if val is not None and winner >= loser:
                d[(winner, loser)] = float(val)
    return d

SCORE_TABLE = {
    "group": _to_dict(_GROUP),
    "ko": _to_dict(_KO),
    "final": _to_dict(_FINAL),
}

# Cap for scorelines beyond the printed table (very rare blowouts).
TABLE_CAP = {"group": 4.5, "ko": 8.25, "final": 11.0}

# --- Detonator games (§18) --------------------------------------------------
DETONATOR_FACTOR = 2.0

# --- Prize ladder (§5) and tie-break (§19) ---------------------------------
PRIZE_LADDER = {1: .23, 2: .15, 3: .125, 4: .105, 5: .09,
                6: .08, 7: .07, 8: .06, 9: .05, 10: .04}

# --- Futures payouts --------------------------------------------------------
WINNER_PAYOUT = {  # §7  (keys = canonical team names, see core/data/teams.normalize)
    "Spain": 20, "France": 20, "England": 26, "Brazil": 33, "Argentina": 34,
    "Portugal": 39, "Germany": 43, "Netherlands": 59, "Norway": 78,
    "United States": 170,
}
SCORER_PAYOUT = {  # §8 (top scorer / melech ha'shearim)
    "Mbappe": 20, "Harry Kane": 21, "Messi": 25, "Haaland": 28,
    "Mikel Oyarzabal": 28, "Lamine Yamal": 30, "Cristiano Ronaldo": 33,
    "Ousmane Dembele": 36, "Vinicius": 39, "Lautaro Martinez": 40,
    "Raphinha": 44, "Kai Havertz": 46, "Julian Alvarez": 48,
    "Romelu Lukaku": 57, "Igor Thiago": 60, "Cody Gakpo": 61,
    "Michael Olise": 65, "Jude Bellingham": 67, "Memphis Depay": 73,
}
CINDERELLA_PAYOUT = {  # §9
    "Congo DR": 15, "Saudi Arabia": 16, "New Zealand": 19, "Cape Verde": 22,
    "Uzbekistan": 23, "Qatar": 23, "Panama": 23, "Jordan": 32, "Iraq": 35,
    "Haiti": 72, "Curacao": 75,
}

# --- Model blend weights (tune via backtest) -------------------------------
# Day-9.26: shifted DC 0.30→0.20, Market 0.50→0.60. Market is the only signal
# where actual money is at stake (calibrated against thousands of bettors);
# DC was fit on pre-tournament data and doesn't update with daily news. The
# news agent's job is to update DC, but it's noisy — leaning into the market
# reduces single-agent risk.
BLEND_WEIGHTS = {"dixon_coles": 0.20, "elo": 0.20, "market": 0.60}

# Knockout-stage penalty-winner trigger: only show "If pens: <team>" on the
# card when the model-blended draw probability is at least this. Tunable so
# we can demand higher confidence later (e.g. mid-tournament after data).
DRAW_PEN_THRESHOLD = 0.15

# --- Direction-confidence gate thresholds (Day-9.26.2) --------------------
# The EV optimizer uses these to decide how strongly to restrict the pick to
# the model's dominant direction. SMOOTH transition between mild_lower and
# strong (no boundary cliff): weight ramps 0→1 across that range.
#
#   weight_to_dom = clip((dom_p - mild_lower) / (strong - mild_lower), 0, 1)
#
#   weight = 0   → pure EV-max across all cells (tossup behavior)
#   weight = 1   → restricted to dom-direction cells, EV-max within  (strong)
#   weight ≈ 0.5 → restricted to dom-direction cells, half-EV-half-P (mild)
#
# KO/Final matches DEMAND earlier protection because:
#   • BASE_POINTS is 1.5x (KO) / 2.0x (Final) — direction-correct floor is
#     worth more, so banking it is more valuable.
#   • Variance on a single match matters MORE when there are fewer matches
#     ahead to average out — and group has 72, KO has 16, SF/Final 4.
# Detonator matches similarly get earlier protection: 2× EV cuts both ways
# and tournament survival prefers the lower-variance direction-locked floor.
GATE_THRESHOLDS = {
    "group":       {"strong": 0.55, "mild_lower": 0.30},
    "ko":          {"strong": 0.50, "mild_lower": 0.28},
    "final":       {"strong": 0.48, "mild_lower": 0.25},
    "detonator":   {"strong": 0.50, "mild_lower": 0.28},   # ANY detonator match
}

# Tied-direction guardrail: when top-two direction probs are within this
# margin, treat as a true tossup regardless of which one is alphabetically
# first. Prevents Python dict-insertion-order tie-breaks from determining
# the pick in close 3-way matchups.
GATE_TIE_MARGIN = 0.02

# News-confidence delta scaling. The clamp ceiling (NEWS_DELTA_CLAMP=0.15)
# limits MAX delta the LLM can push; this scales how much of the clamped
# value is actually applied based on the LLM's self-reported confidence.
# A low-confidence -0.15 becomes -0.045 (0.3× scale); a high-confidence
# -0.15 stays -0.15 (1.0× scale). Defends against confident-looking but
# weakly-grounded LLM calls flipping the gate decision.
NEWS_CONFIDENCE_SCALE = {"low": 0.30, "medium": 0.70, "high": 1.00}
