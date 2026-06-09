"""Day-9.23: live end-to-end card runner — fires build_card with REAL APIs.

Burns a small budget per run (≈ 1 odds_api + 3 api-football + 5-7 Brave + 1 LLM)
and prints the full audit trail:

  • Pre-flight budgets per provider (aborts if any over 90% used)
  • build_card() run with --window controlling the simulated lock
  • Card audit: signals_used / signals_failed / ev_pathway / pick / EV
  • News audit: provider / fallbacks_used / parse_tier / brave_gate
  • Rendered card body (Telegram-shape)
  • Post-flight budgets

Use this to validate the news agent + odds pipeline on real WC2026 data
BEFORE the daemon starts firing scoring-decisive cards in production.

  PYTHONPATH=. .venv/bin/python tools/run_one_card_live.py "Mexico" "South Africa" --window T-7m

Will NOT persist to predictions table (passes conn=None) so it's
side-effect-free — re-runs won't pollute the DB with synthetic snapshots.
"""
from __future__ import annotations
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _budget(provider: str) -> tuple[int, int | None, float]:
    """Return (used, budget_or_None, fraction)."""
    try:
        from core.obs.cost import ledger
        q = ledger().quota_status(provider)
        used = int(q.get("used") or 0)
        budget = q.get("budget")
        frac = (used / budget) if budget else 0.0
        return used, budget, frac
    except Exception:                                       # noqa: BLE001
        return 0, None, 0.0


def _print_budgets(label: str) -> bool:
    """Returns False if any monetary provider is at >= 90% — caller aborts."""
    print(f"  {label}:")
    over = False
    for p in ("api_football", "brave_search", "odds_api", "gemini"):
        used, budget, frac = _budget(p)
        flag = ""
        if budget and frac >= 0.9:
            flag = "  🛑 OVER 90%"
            over = True
        elif budget and frac >= 0.7:
            flag = "  ⚠ over 70%"
        budget_s = str(budget) if budget else "∞"
        print(f"    {p:<14} {used}/{budget_s}{flag}")
    return not over


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="run_one_card_live")
    p.add_argument("home")
    p.add_argument("away")
    p.add_argument("--window", default="T-7m",
                   choices=["T-24h", "T-60m", "T-15m", "T-7m"])
    p.add_argument("--force", action="store_true",
                   help="Run even if any provider is over 90% budget")
    args = p.parse_args(argv)

    print()
    print(f"  ╔════════════════════════════════════════════════════════════╗")
    print(f"  ║  Live card run: {args.home} vs {args.away}  ({args.window})")
    print(f"  ╚════════════════════════════════════════════════════════════╝")
    print()
    safe = _print_budgets("Budgets BEFORE")
    if not safe and not args.force:
        print()
        print("  ⛔ Refusing to run — a provider is over 90% budget. "
              "Use --force to override.")
        return 1
    print()

    # Build a real match dict from the local DB
    from store.db import connect
    conn = connect()
    row = conn.execute(
        "SELECT match_id, utc_kickoff, stage, grp, home, away, detonator "
        "FROM matches WHERE home=? AND away=?",
        (args.home, args.away)).fetchone()
    if not row:
        print(f"  ⛔ Match not found in local matches table. "
              f"Run football_data.refresh first.")
        return 2
    mid, ko, stage, grp, home, away, det = row
    match = {"match_id": mid, "utc_kickoff": ko, "stage": stage,
             "group": grp, "home": home, "away": away,
             "detonator": bool(det), "_window": args.window}

    print(f"  Match: {home} vs {away}  KO={ko}  stage={stage}  "
          f"detonator={bool(det)}")
    print()

    from core.decision.build_card import build_card
    print(f"  ── Running build_card (this WILL call live APIs) ──")
    # conn=None → DOES NOT persist, keeps the run side-effect-free
    card = build_card(match, conn=None, window=args.window)
    print(f"  build_card returned.")
    print()

    # ── Audit trail ──
    print(f"  ── Audit ──")
    print(f"  signals_used:    {card.get('signals_used')}")
    print(f"  signals_failed:  {card.get('signals_failed')}")
    print(f"  failure_reasons: {card.get('failure_reasons')}")
    print(f"  ev_pathway:      {card.get('ev_pathway')}")
    print(f"  pick_direction:  {card.get('pick_direction')}")
    print(f"  pick_exact:      {card.get('pick_exact_score')}")
    print(f"  modal_score:     {card.get('modal_score')}")
    print(f"  EV:              {card.get('expected_points')}")
    print(f"  locked_odds:     {card.get('locked_odds')}")
    print(f"  model_prob:      {card.get('model_prob')}")
    print()

    print(f"  ── News audit ──")
    print(f"  news_provider:           {card.get('news_provider')!r}")
    print(f"  news_fallbacks_used:     {card.get('news_fallbacks_used')}")
    print(f"  news_parse_tier:         {card.get('news_parse_tier')!r}")
    print(f"  news_failure:            {card.get('news_failure')!r}")
    print(f"  news_brave_gate:         {card.get('news_brave_gate')!r}")
    print(f"  news_ctx_failures:       {card.get('news_ctx_failures')}")
    print(f"  news_home_delta:         {card.get('news_home_delta')}")
    print(f"  news_away_delta:         {card.get('news_away_delta')}")
    print(f"  news_confidence:         {card.get('news_confidence')!r}")
    print(f"  news_home_delta_clamped: {card.get('news_home_delta_clamped')}")
    print(f"  news_away_delta_clamped: {card.get('news_away_delta_clamped')}")
    print()

    print(f"  ── friend_picks_section ──")
    fps = card.get("friend_picks_section")
    if fps:
        for ln in fps.splitlines():
            print(f"    {ln}")
    else:
        print(f"    (none — no FRIEND_PARTICIPANTS set or no match details)")
    print()

    # ── Rendered ──
    from core.delivery.base import render_card
    body = render_card(card)
    print(f"  ── Rendered (Telegram-shape, {len(body)} chars / 4096 cap) ──")
    print()
    for ln in body.splitlines():
        print(f"    {ln}")
    print()
    if len(body) > 4096:
        print(f"  🛑 RENDERED BODY EXCEEDS TELEGRAM 4096-CHAR LIMIT")
    print()

    _print_budgets("Budgets AFTER")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
