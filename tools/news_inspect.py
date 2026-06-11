"""Day-9.25: deep-dive inspector for the news_agent's reasoning on one match.

Surfaces EVERY input + output of the news pipeline so you can answer:
  "Why did Gemini decide +0.15 home delta for Mexico v South Africa T-24h?"

Specifically shows:
  1. The 3 Brave queries generated for the window
  2. Each Brave query's top results (titles + snippets, with dates)
  3. The full assembled context that was sent to the LLM (truncated to 4kb)
  4. The exact system prompt (rubric, schema, examples)
  5. The Gemini response: home/away deltas, confidence, NOTES (per-delta
     justification), DISCARDED_SOURCES (sources ignored + why)
  6. The provider chain visited (fallbacks, error reasons)
  7. The final clamped output

Burns ~1 LLM call + 3 Brave queries (same cost as a normal T-24h card).

Usage:
  PYTHONPATH=. .venv/bin/python tools/news_inspect.py Mexico "South Africa" --window T-24h
  PYTHONPATH=. .venv/bin/python tools/news_inspect.py "South Korea" Czechia --window T-24h
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _banner(s: str):
    print(f"\n  {'─' * 4} {s} {'─' * (60 - len(s))}")


def _trim(s: str, n: int = 200) -> str:
    if not s:
        return "(empty)"
    s = str(s)
    return s if len(s) <= n else s[:n] + " …"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="news_inspect")
    p.add_argument("home")
    p.add_argument("away")
    p.add_argument("--window", default="T-60m",
                   choices=["T-24h", "T-60m", "T-15m", "T-7m"])
    p.add_argument("--utc-kickoff", default=None,
                   help="ISO UTC kickoff. If omitted, defaults to NOW (only "
                        "affects query date stamping).")
    args = p.parse_args(argv)

    from orchestrator.agents.news_agent import (
        SYSTEM, search_queries, gather_context, analyze_safe, context_meta,
    )
    from core.llm.router import LLMRouter

    home, away = args.home, args.away
    kickoff = args.utc_kickoff or datetime.now(timezone.utc).isoformat()
    match = {"home": home, "away": away, "stage": "Group", "group": "A",
              "utc_kickoff": kickoff,
              "kickoff_local": "(inspector — local TZ irrelevant)"}

    print()
    print(f"  ╔════════════════════════════════════════════════════════════╗")
    print(f"  ║  News inspector: {home} vs {away}  ({args.window})")
    print(f"  ║  Stage=Group, kickoff={kickoff}")
    print(f"  ╚════════════════════════════════════════════════════════════╝")

    # ──── 1. The 3 (or N) Brave queries the agent would generate ────
    _banner("1. Brave queries (template per window)")
    qs = search_queries(home, away, kickoff_utc=kickoff,
                         stage="Group", group="A", window=args.window)
    for i, q in enumerate(qs, 1):
        print(f"    q{i}: {q!r}")

    # ──── 2. Live gather_context — calls Brave + API-Football for real ────
    _banner("2. Live gather_context — calling Brave + API-Football (real APIs)")
    context_text = gather_context(match, window=args.window)
    meta = context_meta()
    print(f"    brave_gate:                    {meta.get('brave_gate')!r}")
    print(f"    sources_ok:                    {meta.get('sources_ok')}")
    print(f"    ctx_failures (sub-source):     {meta.get('ctx_failures')}")
    print(f"    context_chars (after trim):    {meta.get('context_chars')}")
    print(f"    context_truncated_chars:       {meta.get('context_truncated_chars')}")

    # ──── 3. The assembled context sent to the LLM ────
    _banner("3. Full context text sent to Gemini")
    for ln in context_text.splitlines():
        print(f"    | {ln}")

    # ──── 4. The system prompt (Layer-4 rubric / schema / examples) ────
    _banner("4. System prompt (Layer-4 rubric + JSON schema + examples)")
    for ln in SYSTEM.splitlines():
        print(f"    | {ln}")

    # ──── 5. Live analyze_safe — the actual LLM call ────
    _banner("5. Live LLM call (analyze_safe — production wrapper)")
    router = LLMRouter()
    out = analyze_safe(home, away, context_text, router=router)

    print(f"    provider (who answered):       {out.get('provider')!r}")
    print(f"    fallbacks_used (tried first):  {out.get('fallbacks_used')}")
    print(f"    fallback_errors:")
    for prov, err in (out.get("fallback_errors") or {}).items():
        print(f"      • {prov}: {err.get('error_class')} — {_trim(err.get('error_message'), 100)}")
    print(f"    parse_tier (strict|repair|…):  {out.get('parse_tier')!r}")
    print(f"    confidence:                    {out.get('confidence')!r}"
          + ("  (DEFAULTED)" if out.get('confidence_was_defaulted') else ""))
    print(f"    home_goal_delta:               {out.get('home_goal_delta')}"
          + ("  (CLAMPED — raw=" + str(out.get('home_delta_raw')) + ")"
             if out.get('home_delta_clamped') else ""))
    print(f"    away_goal_delta:               {out.get('away_goal_delta')}"
          + ("  (CLAMPED — raw=" + str(out.get('away_delta_raw')) + ")"
             if out.get('away_delta_clamped') else ""))

    # ──── 6. The Gemini reasoning — the WHY ────
    _banner("6. WHY — Gemini's notes (per-delta justification)")
    notes = out.get("notes") or []
    if not notes:
        print("    (no notes — Gemini returned an empty list; this can mean "
              "either no signals found OR the model skipped justification)")
    for i, n in enumerate(notes, 1):
        print(f"    note{i}: {n}")

    _banner("7. Discarded sources — what Gemini saw but IGNORED, and why")
    discarded = out.get("discarded_sources") or []
    if not discarded:
        print("    (none listed)")
    for i, d in enumerate(discarded, 1):
        print(f"    skip{i}: {d}")

    # ──── 8. Raw excerpt — only set on parse_tier in (regex_repair, failed) ────
    if out.get("raw_excerpt"):
        _banner("8. Raw LLM excerpt (parse needed repair OR failed)")
        for ln in out["raw_excerpt"].splitlines():
            print(f"    | {ln}")

    # ──── 9. Final card-ready deltas + how build_card would apply them ────
    _banner("9. How build_card would apply these deltas")
    h = out.get("home_goal_delta", 0.0)
    a = out.get("away_goal_delta", 0.0)
    print(f"    DC's expected goals (home, away) are SHIFTED by:")
    print(f"      home_xg += {h:+.3f}  → small {'boost' if h > 0 else 'cut' if h < 0 else 'no change'}")
    print(f"      away_xg += {a:+.3f}  → small {'boost' if a > 0 else 'cut' if a < 0 else 'no change'}")
    print(f"    The shift feeds into the Poisson score_matrix, which changes the")
    print(f"    direction probabilities (H/D/A) and ultimately the EV pick.")
    print(f"    Net effect on the card: small but non-zero (rubric caps it at "
          f"±{0.6:.1f}).")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
