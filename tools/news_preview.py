"""Day-9.23: live news_agent inspection — see exactly what the LLM sees.

Fires `gather_context()` + `analyze()` against a real match and prints:

  1. Budget snapshot for each provider BEFORE the run
  2. What api-football returned (lineups, injuries)
  3. What Brave returned (queries, snippets, dropped-irrelevant counts)
  4. The exact SYSTEM + USER prompt the LLM received (truncated)
  5. The LLM's raw JSON response
  6. The parsed deltas + parse_tier + clamp flags
  7. Budget snapshot AFTER

Used to verify pre-tournament that the news agent is doing something
meaningful BEFORE the real T-7m lock fires. Costs ≤ 8 credits total:
4-7 Brave + ~3 api-football + 1 LLM call.

Usage:
    PYTHONPATH=. .venv/bin/python tools/news_preview.py "Mexico" "South Africa"
    PYTHONPATH=. .venv/bin/python tools/news_preview.py "Mexico" "South Africa" --window T-60m
    PYTHONPATH=. .venv/bin/python tools/news_preview.py "Mexico" "South Africa" --no-llm

`--no-llm` shows ONLY the gathered context (saves the LLM call —
useful when you want to inspect what would be sent without burning a Gemini credit).
"""
from __future__ import annotations
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _budget(provider: str) -> str:
    try:
        from core.obs.cost import ledger
        q = ledger().quota_status(provider)
        budget = q.get("budget")
        used = q.get("used") or 0
        if not budget:
            return f"used={used}  (no cap)"
        return f"used={used}/{budget}  ({used / budget * 100:.1f}%)"
    except Exception as e:                                  # noqa: BLE001
        return f"unknown ({e})"


def _print_budgets(label: str):
    print(f"  {label}:")
    for p in ("api_football", "brave_search", "odds_api", "gemini", "claude", "openai"):
        print(f"    {p:<14} {_budget(p)}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="news_preview")
    p.add_argument("home")
    p.add_argument("away")
    p.add_argument("--window", default="T-60m",
                   choices=["T-24h", "T-60m", "T-15m", "T-7m"],
                   help="Which window to simulate (affects query templates)")
    p.add_argument("--no-llm", action="store_true",
                   help="Skip analyze() — only fetch + print gathered context")
    args = p.parse_args(argv)

    print()
    print(f"  ╔════════════════════════════════════════════════════════════╗")
    print(f"  ║  News preview: {args.home} vs {args.away}  ({args.window})")
    print(f"  ╚════════════════════════════════════════════════════════════╝")
    print()
    _print_budgets("Budgets BEFORE")
    print()

    # Build a synthetic match dict in the shape news_agent expects
    try:
        from store.db import connect
        conn = connect()
        row = conn.execute(
            "SELECT match_id, utc_kickoff, stage FROM matches WHERE home=? AND away=?",
            (args.home, args.away)).fetchone()
    except Exception:                                       # noqa: BLE001
        row = None
    match = {
        "match_id": row[0] if row else 0,
        "utc_kickoff": row[1] if row else "2026-06-11T19:00:00+00:00",
        "stage": row[2] if row else "Group",
        "home": args.home, "away": args.away,
    }
    print(f"  Match: {match['home']} vs {match['away']}  "
          f"KO={match['utc_kickoff']}  match_id={match['match_id']}")
    print()

    # ──────────────── gather_context ────────────────
    from orchestrator.agents.news_agent import (
        gather_context, context_meta, analyze, analyze_safe)
    print(f"  ── 1. gather_context({args.window}) ──")
    ctx = gather_context(match, window=args.window)
    meta = context_meta()
    print(f"  Sources OK:    {meta.get('sources_ok', [])}")
    print(f"  Sources failed: {meta.get('ctx_failures', [])}")
    print(f"  Brave gate:    {meta.get('brave_gate')!r}")
    print(f"  Context chars: {meta.get('context_chars', 0)}")
    print(f"  Truncated:     {meta.get('context_truncated_chars', 0)}")
    print()
    print(f"  ── 2. Context body (what the LLM will see) ──")
    print()
    # Indent each line for clarity
    for ln in ctx.splitlines():
        print(f"    {ln}")
    print()

    # ──────────────── analyze ────────────────
    if args.no_llm:
        print(f"  ── 3. analyze() SKIPPED (--no-llm) ──")
    else:
        print(f"  ── 3. analyze() — LLM call ──")
        result = analyze_safe(match, context=ctx, window=args.window)
        print(f"  Provider:      {result.get('provider')!r}")
        print(f"  Fallbacks used: {result.get('fallbacks_used', [])}")
        print(f"  Failure:        {result.get('failure')!r}")
        print(f"  Parse tier:     {result.get('parse_tier')!r}")
        if result.get('raw_excerpt'):
            print(f"  Raw excerpt:    {result['raw_excerpt'][:200]!r}")
        print()
        print(f"  Deltas:")
        print(f"    home: {result.get('home', 0):+.3f}   "
              f"(clamped from {result.get('home_delta_raw', 'n/a')})")
        print(f"    away: {result.get('away', 0):+.3f}   "
              f"(clamped from {result.get('away_delta_raw', 'n/a')})")
        print(f"  Confidence:     {result.get('confidence')!r}"
              + (" (defaulted)" if result.get('confidence_was_defaulted') else ""))
        notes = result.get('notes') or []
        if notes:
            print(f"  Notes ({len(notes)}):")
            for n in notes[:5]:
                print(f"    • {n}")
        if result.get('schema_error'):
            print(f"  ⚠ schema_error: {result['schema_error']!r}")

    print()
    _print_budgets("Budgets AFTER")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
