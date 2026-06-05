"""Day-3 calibration command — the one-shot you run once historical results are
wired. It loads results, fits Dixon-Coles, builds backtest samples (DC vs Elo vs
market for held-out matches), tunes the blend weights to minimize log-loss, and
prints the recommended `config.rules.BLEND_WEIGHTS` + a calibration report.

    python -m tools.calibrate

You provide two injectables on your machine (see results_io / an odds archive):
  results_fetch() -> historical result rows
  samples()       -> [{"dc","elo","market","actual"}] for held-out matches
This module's `run(...)` takes them as args so it is unit-tested offline.
"""
from __future__ import annotations
from core.data.results_io import historical_results
from core.models.fit import fit_from_results, expected_goals_fn
from core.models import backtest as bt


def run(results_fetch, samples) -> dict:
    """Returns the recommended weights + metrics + calibration. Pure given inputs."""
    results = historical_results(fetch=results_fetch)
    strengths = fit_from_results(results)               # fitted DC (proves data is usable)
    tune = bt.tune_blend_weights(samples)
    cal = bt.calibration(samples, tune["best_weights"])
    return {"n_results": len(results), "fitted_teams": len(strengths["teams"]),
            "recommended_weights": tune["best_weights"],
            "metrics": tune["best_metrics"], "market_baseline": tune["market_baseline"],
            "beats_market": tune["beats_market"], "calibration": cal}


def report(out: dict) -> str:
    lines = [f"results: {out['n_results']}  fitted teams: {out['fitted_teams']}",
             f"recommended BLEND_WEIGHTS = {out['recommended_weights']}",
             f"log-loss {out['metrics']['log_loss']} vs market {out['market_baseline']['log_loss']} "
             f"(beats market: {out['beats_market']})",
             "calibration (avg_pred vs obs_rate):"]
    for b in out["calibration"]:
        lines.append(f"  {b['range']}: pred {b['avg_pred']} | obs {b['obs_rate']} (n={b['n']})")
    return "\n".join(lines)


if __name__ == "__main__":
    # On your machine, replace these with the live sources (see module docstring).
    raise SystemExit("Wire results_fetch + samples (historical results + held-out "
                     "match probabilities), then call tools.calibrate.run(...).")
