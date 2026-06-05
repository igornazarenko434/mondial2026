"""Single match-window pipeline — the unit the scheduler runs.

Ties everything together with the reliability + observability guarantees the user
asked for:
  • one correlation id + trace per run (obs.run)
  • retry-with-backoff on transient data/odds errors, source fallback
  • a run-status row recording ok / failed / fell_back / card_delivered
  • the recommendation card DELIVERED to the configured channels
  • LOUD on failure: if anything breaks, an alert is sent — never silent

`build_card` is injected so this is fully unit-testable without network. The
default wires the real model; the scheduler calls `process_match`.
"""
from __future__ import annotations
from typing import Callable
from core import obs, delivery
from core.obs.runs import runs
from core.reliability import retry
from core.obs.logging import get_logger
from core.decision.strategy import recommend_to_win

log = get_logger("pipeline")


def process_match(match: dict, window: str, build_card: Callable[[dict], dict],
                  *, max_attempts: int = 3,
                  strategy_context: dict | None = None,
                  strategy_tilt: float | None = None) -> dict:
    """Run one (match, window) job. Returns a status dict.

    match: {"match_id", "home", "away", "stage", "detonator", ...}
    build_card(match) -> recommendation card dict (raises on data failure).

    The win-probability strategy layer is CONNECTED here but dormant by default:
    with no `strategy_context` (or STRATEGY_TILT=0) the card is the pure-EV pick.
    Pass a standings context (see store.repo.standings_context) + a tilt later in
    the tournament to enable position/variance management. It is fallback-safe —
    it can only refine the pick, never break the card.
    """
    mid = match.get("match_id")
    label = f"match-{mid}-{window}"
    ledger = runs()
    with obs.run(label):
        run_id = ledger.start(mid, window, correlation_id=label)
        attempts = {"n": 0}

        @retry(max_attempts=max_attempts)
        def _build():
            attempts["n"] += 1
            return build_card(match)

        try:
            card = _build()
        except Exception as e:  # noqa: BLE001 - terminal: record + alert, stay loud
            stage = obs.stage_of(e)                    # which stage failed
            detail = f"[{stage}] {e}"
            log.error("pipeline failed for %s at stage '%s': %s", label, stage, e)
            ledger.finish(run_id, "failed", attempts=attempts["n"], detail=detail)
            delivery.alert(f"Pipeline FAILED — {match.get('home')} vs {match.get('away')}",
                           f"{window} [stage: {stage}]: {e}")
            return {"status": "failed", "match_id": mid, "window": window,
                    "stage": stage, "error": str(e)}

        # win-probability strategy layer — dormant unless context+tilt given
        card = recommend_to_win(card, strategy_context, strategy_tilt)
        if card.get("strategy", {}).get("deviated_from_ev"):
            log.info("strategy tilt re-picked %s (EV-optimal was %s)",
                     card["pick_exact_score"], card["strategy"]["ev_optimal_score"])

        delivered = delivery.deliver_card(card)
        ledger.finish(run_id, "ok", attempts=attempts["n"],
                      provider=card.get("odds_source"), card_delivered=delivered,
                      detail=None if delivered else "card built but delivery failed")
        if not delivered:
            delivery.alert(f"Delivery FAILED — {match.get('home')} vs {match.get('away')}",
                           f"{window}: card computed but no channel accepted it")
        log.info("pipeline ok for %s (delivered=%s)", label, delivered)
        return {"status": "ok", "match_id": mid, "window": window,
                "delivered": delivered, "card": card}


def daily_summary(hours: int = 24) -> dict:
    """Build + push the health summary so silence never means 'unknown'."""
    s = runs().summary(hours)
    delivery.health(s)
    return s
