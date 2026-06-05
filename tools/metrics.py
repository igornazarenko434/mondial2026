"""Metrics CLI — watch what the system did, per game / provider / window.

The SQLite ledgers (api_calls, runs) ARE the metric store — every call and run is
persisted with a correlation id (e.g. 'match-401-T7m'), latency, tokens and cost.
So you can query metrics for any game with zero extra infrastructure.

  python -m tools.metrics                      # overall (24h) + per-provider
  python -m tools.metrics match-401-T-7m       # one game/run drill-down
"""
from __future__ import annotations
import sys
from core.obs.cost import ledger
from core.obs.runs import runs


def overall(hours: int = 24) -> dict:
    led = ledger()
    providers = {}
    for p in ("football_data", "odds_api", "api_football", "claude", "gemini", "openai"):
        u = led.usage(p)
        if u["calls"]:
            providers[p] = u
    return {"runs": runs().summary(hours), "providers": providers,
            "quota": {p: led.quota_status(p) for p in ("odds_api", "api_football", "gemini")}}


def per_game(correlation_id: str) -> dict:
    return ledger().metrics_for(correlation_id)


def _print(d, indent=0):
    for k, v in d.items():
        if isinstance(v, dict):
            print("  " * indent + f"{k}:"); _print(v, indent + 1)
        else:
            print("  " * indent + f"{k}: {v}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(f"metrics for {sys.argv[1]}:"); _print(per_game(sys.argv[1]))
    else:
        _print(overall())
