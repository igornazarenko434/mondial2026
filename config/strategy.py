"""Win-probability strategy knobs.

The EV optimizer maximizes EXPECTED POINTS (→ expected final total). Winning a
top-heavy prize pool is a different objective: maximize P(finish 1st), which means
taking variance when behind and protecting a lead when ahead. This layer is
OPT-IN: with TILT=0 the system behaves exactly like the pure-EV optimizer.
"""
import os

# 0.0 = pure EV (max expected total) — RECOMMENDED FOR NOW (no standings logic).
# Raise to 0.3–0.6 LATER in the tournament to add the position/variance tilt.
DEFAULT_TILT = float(os.environ.get("STRATEGY_TILT", "0.0"))
# how many top-EV candidates the strategy may choose among (stay near-optimal)
TOP_K = int(os.environ.get("STRATEGY_TOP_K", "5"))
# typical points achievable per remaining game — scales the "can I still catch up?" math
SWING = float(os.environ.get("STRATEGY_SWING", "6.0"))
