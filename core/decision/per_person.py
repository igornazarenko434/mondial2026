"""Day-9.24: per-person EV-optimization on the same shared P(score) matrix.

WHY
===

The expensive parts of build_card (Dixon-Coles fit, Elo, market odds,
news_agent LLM call) all produce ONE artifact: a P(score) matrix +
locked odds. Running ev_optimizer.recommend on that matrix is
microseconds of pure Python math. So computing DIFFERENT picks for
DIFFERENT tracked people (each with their own strategy tilt + their
own standings position) costs effectively nothing.

This module is that cheap second step. It reads the same `card` produced
by build_card's main pick, then for each tracked participant:

  • Looks up THEIR standings_context (their gap to leader, points, etc.)
  • Applies THEIR strategy tilt (per-person override or global default)
  • Re-runs `strategy.recommend_to_win(card, context, tilt)` to get THEIR pick

Returns a list of {name, tilt, rank, pick_direction, pick_exact_score,
expected_points, deviated_from_ev}. The render layer turns that into a
"🎯 Per-person suggestions" block on the card.

CONFIGURATION
=============

Two env vars (both optional; both default to NO override → backwards-compat):

  STRATEGY_TILT          — global default for every tracked person
  STRATEGY_OVERRIDES     — JSON dict of {displayName: tilt} overrides.
                           Example: '{"Vaadia": 0.4, "Alon": 0}'

If neither is set or every override matches the global, no per-person
section is rendered (the main card is already the same for everyone).

The section is also gracefully omitted when:
  • build_card couldn't compute ranked_alternatives (modal fallback)
  • Standings table is empty (pre-tournament)
  • Any per-person compute raises (we never break the main pick)

DESIGN PRINCIPLE
================

NEVER modify the main `card["pick_exact_score"]` etc. That stays the
operator's authoritative pick (MY_PARTICIPANT, MY tilt). The per-person
output is purely informational + appended as a separate section.
"""
from __future__ import annotations
import json
import os
from typing import Any

from core.obs.logging import get_logger

log = get_logger("decision.per_person")


def _parse_overrides() -> dict[str, float]:
    """Parse STRATEGY_OVERRIDES env JSON. Returns {} on missing/invalid."""
    raw = os.environ.get("STRATEGY_OVERRIDES", "").strip()
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        if not isinstance(d, dict):
            log.warning("STRATEGY_OVERRIDES is JSON but not a dict; ignoring")
            return {}
        return {str(k): float(v) for k, v in d.items()}
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        log.warning("STRATEGY_OVERRIDES invalid JSON: %s — ignoring", e)
        return {}


def _global_tilt() -> float:
    try:
        return float(os.environ.get("STRATEGY_TILT", "0") or "0")
    except ValueError:
        return 0.0


def _tilt_for(name: str, overrides: dict[str, float]) -> float:
    """Per-person tilt resolution: explicit override → global STRATEGY_TILT."""
    if name in overrides:
        return max(0.0, min(1.0, overrides[name]))
    return max(0.0, min(1.0, _global_tilt()))


def _enabled(tracked: list[str], overrides: dict[str, float]) -> bool:
    """Return True iff at least one tracked person has a non-default tilt.
    If all picks would be IDENTICAL to the main card, no section is needed."""
    if not tracked or len(tracked) < 2:
        # Single-person tracking (just the operator) → no point in a
        # 'per-person' section: the main card IS their pick.
        return bool(overrides)            # only render if overrides explicit
    gtilt = _global_tilt()
    # Any person's tilt differs from the global default → render section.
    # (Even if every override equals the global, no value-add.)
    return any(_tilt_for(name, overrides) != gtilt or name in overrides
               for name in tracked)


def compute_per_person_suggestions(card: dict, conn: Any,
                                    *, tracked: list[str] | None = None
                                    ) -> list[dict] | None:
    """Run the strategy layer N times — once per tracked person — using
    each person's own standings_context + tilt. Returns a list of
    {name, tilt, rank, pick_direction, pick_exact_score, expected_points,
    deviated_from_ev} suitable for rendering. Returns None when the
    section shouldn't render (gracefully degrade).

    Never raises. On any error returns None — the main card stays intact.
    """
    try:
        # Lazy import to avoid circulars
        from core.reporting import people
        tracked = tracked if tracked is not None else people.tracked_participants()
        overrides = _parse_overrides()

        if not _enabled(tracked, overrides):
            return None

        # Card must have ranked_alternatives — without them the strategy
        # layer has no menu of candidate picks to choose from.
        if not card.get("ranked_alternatives"):
            log.debug("per_person section skipped: no ranked_alternatives "
                      "on card (probably modal-fallback)")
            return None

        # We need the Negev rank for the display. Pull from Negev once.
        rank_by_name: dict[str, int] = {}
        try:
            from integrations import negev_toto_mcp as ntm
            from core import obs
            with obs.external_call("negev_toto", "get_standings"):
                rows = ntm.toto_get_standings(include_bots=True)
            rank_by_name = {r["player"]: r.get("rank")
                             for r in rows if r.get("player")}
        except Exception as e:                      # noqa: BLE001
            log.warning("per_person rank lookup failed: %s — proceeding "
                        "without rank", e)

        from core.decision.strategy import recommend_to_win
        from store import repo

        out = []
        for name in tracked:
            tilt = _tilt_for(name, overrides)
            ctx = None
            if conn is not None:
                try:
                    ctx = repo.standings_context(conn, me=name)
                except Exception as e:              # noqa: BLE001
                    log.warning("standings_context for %r failed: %s", name, e)
                    ctx = None
            # If we have NO context AND NO tilt, the call returns the
            # main card's pick unchanged → still informative (shows the
            # baseline). Render it.
            rec = recommend_to_win(card, context=ctx, tilt=tilt)
            strat = rec.get("strategy") or {}
            out.append({
                "name": name,
                "tilt": tilt,
                "rank": rank_by_name.get(name),
                "pick_direction": rec.get("pick_direction"),
                "pick_exact_score": rec.get("pick_exact_score"),
                "expected_points": rec.get("expected_points"),
                "deviated_from_ev": bool(strat.get("deviated_from_ev")),
                "ctx_present": ctx is not None,
            })
        return out
    except Exception as e:                          # noqa: BLE001
        log.warning("compute_per_person_suggestions raised: %s; section omitted", e)
        return None


def render_section(suggestions: list[dict] | None,
                   home: str, away: str,
                   detonator: bool = False) -> str | None:
    """Render the per-person block. Returns None if no rows."""
    if not suggestions:
        return None
    lines = ["🎯 Per-person suggestions"]
    for s in suggestions:
        pick = s.get("pick_exact_score") or {}
        h, a = pick.get("home", "?"), pick.get("away", "?")
        ev = s.get("expected_points")
        ev_s = f"EV {ev:.2f}" if isinstance(ev, (int, float)) else "EV ?"
        if isinstance(ev, (int, float)) and detonator:
            ev_s += f" → ×2 {ev * 2:.2f}"
        rank = s.get("rank")
        rank_s = f"rank {rank}" if rank else "rank ?"
        tilt = s.get("tilt", 0)
        dev = "  ⚡" if s.get("deviated_from_ev") else ""
        lines.append(f"  👤 {s['name']}  (tilt {tilt:.2f}, {rank_s}):  "
                      f"{home} {h} — {away} {a}   {ev_s}{dev}")
    return "\n".join(lines)
