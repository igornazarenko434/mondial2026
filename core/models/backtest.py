"""Backtest + blend-weight tuning (Day 3) — turns the 'sensible defaults' for the
blend into *validated* weights.

You feed it samples, each holding the three sources' 1X2 probabilities for a past
match (Dixon-Coles, Elo, market) and the actual outcome. It scores any blend with
proper scoring rules (log-loss is the primary; lower = better-calibrated), reports
calibration, and grid-searches the blend weights that minimize log-loss — which is
what you then put in config.rules.BLEND_WEIGHTS.

Pure Python — no model/network deps — so it's unit-tested offline.
"""
from __future__ import annotations
import math

OUTCOMES = ("H", "D", "A")


def blend(dc: dict, elo: dict, market: dict, w: dict) -> dict:
    """Weighted, renormalized blend of three 1X2 distributions."""
    t = {o: w.get("dixon_coles", 0) * dc.get(o, 0)
            + w.get("elo", 0) * elo.get(o, 0)
            + w.get("market", 0) * market.get(o, 0) for o in OUTCOMES}
    s = sum(t.values())
    return {o: t[o] / s for o in OUTCOMES} if s > 0 else {o: 1 / 3 for o in OUTCOMES}


def log_loss(prob: dict, actual: str) -> float:
    """-log(p(actual)); the primary metric (lower = better)."""
    return -math.log(max(1e-12, min(1.0, prob.get(actual, 0.0))))


def brier(prob: dict, actual: str) -> float:
    """Multiclass Brier score over {H,D,A} (lower = better)."""
    return sum((prob.get(o, 0.0) - (1.0 if o == actual else 0.0)) ** 2 for o in OUTCOMES)


def evaluate(samples: list[dict], w: dict) -> dict:
    """Mean log-loss & Brier of the blended probabilities over the samples.
    Each sample: {"dc":{H,D,A}, "elo":{...}, "market":{...}, "actual":"H|D|A"}."""
    if not samples:
        return {"n": 0, "log_loss": float("inf"), "brier": float("inf")}
    ll = br = 0.0
    for s in samples:
        p = blend(s["dc"], s["elo"], s["market"], w)
        ll += log_loss(p, s["actual"])
        br += brier(p, s["actual"])
    n = len(samples)
    return {"n": n, "log_loss": round(ll / n, 4), "brier": round(br / n, 4)}


def market_baseline(samples: list[dict]) -> dict:
    """Score the market alone — the bar the blend must beat to add value."""
    return evaluate(samples, {"market": 1.0})


def _weight_grid(step: float = 0.1, market_min: float = 0.3):
    """Candidate weight triples summing to 1 (market kept >= market_min — markets
    are hard to beat, so we don't search degenerate market-light blends)."""
    out = []
    n = round(1 / step)
    for i in range(n + 1):
        for j in range(n + 1 - i):
            dc, elo = i * step, j * step
            mk = round(1 - dc - elo, 5)
            if mk + 1e-9 >= market_min:
                out.append({"dixon_coles": round(dc, 5), "elo": round(elo, 5), "market": mk})
    return out


def tune_blend_weights(samples: list[dict], grid=None) -> dict:
    """Grid-search the weights minimizing log-loss. Returns best weights, its
    metrics, and the market baseline for comparison."""
    grid = grid or _weight_grid()
    scored = [(w, evaluate(samples, w)) for w in grid]
    best_w, best_m = min(scored, key=lambda t: t[1]["log_loss"])
    return {"best_weights": best_w, "best_metrics": best_m,
            "market_baseline": market_baseline(samples),
            "beats_market": best_m["log_loss"] <= market_baseline(samples)["log_loss"]}


def calibration(samples: list[dict], w: dict, bins: int = 5) -> list[dict]:
    """Reliability check: bucket predicted P(home win) and compare to the observed
    home-win rate. Well-calibrated → predicted ≈ observed in each bucket."""
    buckets = [{"lo": k / bins, "hi": (k + 1) / bins, "pred": [], "obs": []} for k in range(bins)]
    for s in samples:
        p = blend(s["dc"], s["elo"], s["market"], w)["H"]
        k = min(bins - 1, int(p * bins))
        buckets[k]["pred"].append(p)
        buckets[k]["obs"].append(1.0 if s["actual"] == "H" else 0.0)
    out = []
    for b in buckets:
        if b["pred"]:
            out.append({"range": f'{b["lo"]:.1f}-{b["hi"]:.1f}', "n": len(b["pred"]),
                        "avg_pred": round(sum(b["pred"]) / len(b["pred"]), 3),
                        "obs_rate": round(sum(b["obs"]) / len(b["obs"]), 3)})
    return out
