"""End-to-end news-agent test harness — REAL Brave + REAL LLM, full trace.

Walks one fixture through the entire news pipeline:

  1. search_queries()         — what queries we'll send
  2. api_football.find_fixture_id / fetch_lineups / fetch_injuries
  3. web_search.web_search_many — actual Brave HTTP calls
  4. gather_context()         — assembled context block (what the LLM sees)
  5. LLMRouter._ordered_available — providers eligible right now
  6. analyze_safe()           — real LLM call
  7. _parse_json_lenient      — strict → regex_repair → failed tier
  8. _validate_and_clamp      — every silent-degradation flag surfaced

Prints (and saves to a report file) EVERY observability field:
  - which queries fired, how many results returned per query
  - per-source ctx_failures + brave_gate reason
  - context char count + truncation
  - LLM provider that answered + fallbacks_used + per-provider errors
  - parse_tier + raw_excerpt (if parse failed)
  - clamped/defaulted/truncated provenance fields
  - cost-ledger impact for this run (calls per provider, tokens, $)

Cost per run (worst case, all fresh):
  ~4 Brave queries (T-60m window default) × 1 credit = 4 credits / 1000 monthly
  ~1 Gemini call × 1 unit = 1 / 1500 daily
  ~0 api-football (cached after first call) / 100 daily
  ~$0 total — all within free tiers.

Usage:
    sudo -u mondial bash -c '
      cd /home/mondial/mondial2026
      set -a && source .env && set +a
      PYTHONPATH=. .venv/bin/python tools/test_news_agent.py
    '

Flags:
  --window {T-24h|T-60m|T-15m|T-7m}   default T-60m (most signal)
  --home / --away / --stage / --group / --utc-kickoff
       Override the default Mexico v South Africa fixture
  --no-llm           Skip the LLM step, just show what context would be sent
  --no-save          Skip writing the report file
  --report-dir PATH  Where to save the report (default reports/)
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ───── helpers ────────────────────────────────────────────────────────────────

class TeeWriter:
    """Echo every print to both stdout AND a file."""
    def __init__(self, fh):
        self.fh = fh
    def __call__(self, *args, **kw):
        print(*args, **kw)
        if self.fh is not None:
            print(*args, **kw, file=self.fh)


def banner(P, title: str) -> None:
    P("\n" + "=" * 78)
    P("  " + title)
    P("=" * 78)


def fmt(value, max_len: int = 80) -> str:
    s = repr(value)
    return s if len(s) <= max_len else s[:max_len - 3] + "..."


# ───── default test fixture ──────────────────────────────────────────────────

DEFAULT_FIXTURE = {
    "match_id": 1489369,          # Negev / api-football fixture id
    "home": "Mexico",
    "away": "South Africa",
    "stage": "Group",
    "group": "A",
    "utc_kickoff": "2026-06-11T19:00:00+00:00",
    "detonator": True,
}


# ───── main ───────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="test_news_agent")
    p.add_argument("--window", choices=["T-24h", "T-60m", "T-15m", "T-7m"],
                   default="T-60m")
    p.add_argument("--home")
    p.add_argument("--away")
    p.add_argument("--stage")
    p.add_argument("--group")
    p.add_argument("--utc-kickoff")
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("--no-save", action="store_true")
    p.add_argument("--report-dir", default="reports")
    args = p.parse_args(argv)

    fixture = dict(DEFAULT_FIXTURE)
    for k in ("home", "away", "stage", "group", "utc_kickoff"):
        v = getattr(args, k.replace("-", "_"))
        if v:
            fixture[k] = v
    window = args.window

    # Open report file
    report_path = None
    fh = None
    if not args.no_save:
        os.makedirs(args.report_dir, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        slug = f"{fixture['home']}_vs_{fixture['away']}_{window}".replace(" ", "_")
        report_path = os.path.join(args.report_dir,
                                    f"news_agent_test_{ts}_{slug}.txt")
        fh = open(report_path, "w")
    P = TeeWriter(fh)

    P("\n  ✻ News-agent end-to-end test harness")
    P(f"  Fixture: {fixture['home']} vs {fixture['away']}")
    P(f"  Window: {window}   stage: {fixture['stage']}   group: {fixture.get('group')}")
    P(f"  Kickoff (UTC): {fixture['utc_kickoff']}")
    P(f"  Detonator: {fixture.get('detonator', False)}")
    if report_path:
        P(f"  Report file: {report_path}")

    # ───── Stage 1 — query generation ──────────────────────────────────────
    banner(P, "§1  search_queries() — what we'll ask Brave")
    from orchestrator.agents.news_agent import (
        search_queries, gather_context, analyze_safe, context_meta,
    )
    queries = search_queries(fixture["home"], fixture["away"],
                              kickoff_utc=fixture["utc_kickoff"],
                              stage=fixture["stage"],
                              group=fixture.get("group"),
                              window=window)
    for i, q in enumerate(queries, 1):
        P(f"  q{i}: {q!r}")
    if not queries:
        P("  (no queries for this window — context will rely on api-football only)")

    # Cost-ledger snapshot BEFORE the run
    from core.obs.cost import ledger as ledger_fn
    ledger = ledger_fn()
    pre = {}
    for prov in ("brave_search", "api_football", "gemini", "claude", "openai"):
        try:
            row = ledger.conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(units),0), COALESCE(SUM(tokens),0) "
                "FROM api_calls WHERE provider=?", (prov,)).fetchone()
            pre[prov] = row
        except Exception:                              # noqa: BLE001
            pre[prov] = (0, 0, 0)

    # ───── Stage 2 — context gathering (real Brave + api-football) ────────
    banner(P, "§2  gather_context() — REAL Brave + api-football calls")
    t0 = time.monotonic()
    try:
        ctx = gather_context(fixture, window=window)
        ctx_dur_ms = (time.monotonic() - t0) * 1000
    except Exception as e:                              # noqa: BLE001
        P(f"  ✗ gather_context raised: {type(e).__name__}: {e}")
        return 2
    meta = context_meta()

    P(f"  ✓ gathered in {ctx_dur_ms:.0f}ms; {len(ctx)} chars assembled")
    P(f"  sources_ok:         {meta.get('sources_ok')}")
    P(f"  ctx_failures:       {meta.get('ctx_failures')}")
    P(f"  brave_gate:         {meta.get('brave_gate')}")
    P(f"  context_chars:      {meta.get('context_chars')}")
    P(f"  truncated_chars:    {meta.get('context_truncated_chars')}")
    P()
    P("  ─── ASSEMBLED CONTEXT (the LLM's input) ───────────────────────────────")
    for line in ctx.split("\n"):
        P("    " + line)
    P("  ─── end context ───────────────────────────────────────────────────────")

    if args.no_llm:
        P("\n  (--no-llm passed; skipping the LLM step)")
        if fh:
            fh.close()
        return 0

    # ───── Stage 3 — LLM router state ─────────────────────────────────────
    banner(P, "§3  LLMRouter chain state — which providers will fire")
    from core.llm.router import LLMRouter
    router = LLMRouter()
    P(f"  Chain (config): {router.chain}")
    available = router._ordered_available()
    P(f"  Available now:  {[p.name for p in available]}")
    if hasattr(router, "_last_skips"):
        P(f"  Will skip:      {router._last_skips}")
    if not available:
        P("  ✗ NO providers available — LLM step will fail")

    # ───── Stage 4 — analyze_safe() ───────────────────────────────────────
    banner(P, "§4  analyze_safe() — real LLM call")
    t0 = time.monotonic()
    result = analyze_safe(fixture["home"], fixture["away"],
                          context_text=ctx, router=router)
    llm_dur_ms = (time.monotonic() - t0) * 1000

    P(f"  ✓ LLM call completed in {llm_dur_ms:.0f}ms")
    P(f"  Provider answered:        {result.get('provider')!r}")
    P(f"  Fallbacks tried first:    {result.get('fallbacks_used')!r}")
    P(f"  Fallback errors:          {result.get('fallback_errors')!r}")
    P(f"  Parse tier:               {result.get('parse_tier')!r}")
    P(f"  JSON-mode fallback used:  {result.get('json_mode_fallback_used')!r}")
    if result.get("raw_excerpt"):
        P(f"  Raw excerpt (parse imperfect):")
        for line in result["raw_excerpt"].split("\n")[:5]:
            P(f"    {line}")
    if result.get("failure"):
        P(f"  ✗ Failure reason:         {result.get('failure')!r}")
        P(f"  ✗ Failure class:          {result.get('failure_class')!r}")

    # ───── Stage 5 — validated output ─────────────────────────────────────
    banner(P, "§5  _validate_and_clamp() — final structured output")
    P(f"  home_goal_delta:          {result.get('home_goal_delta')}")
    P(f"  away_goal_delta:          {result.get('away_goal_delta')}")
    P(f"  confidence:               {result.get('confidence')!r}")
    P(f"  notes:                    {result.get('notes')}")
    P(f"  discarded_sources:        {result.get('discarded_sources')}")
    P()
    P("  Provenance / silent-degradation flags:")
    for k in ("home_delta_raw", "away_delta_raw",
              "home_delta_clamped", "away_delta_clamped",
              "delta_parse_error", "confidence_was_defaulted",
              "confidence_raw", "notes_truncated",
              "notes_original_count", "notes_format_error",
              "schema_error"):
        if k in result and result[k] not in (None, False):
            P(f"    {k}: {result[k]!r}")

    # ───── Stage 6 — cost-ledger impact ───────────────────────────────────
    banner(P, "§6  Cost-ledger impact of this run")
    P(f"  {'provider':<14} {'calls':>5} {'units':>6} {'tokens':>8}")
    for prov in ("brave_search", "api_football", "gemini", "claude", "openai"):
        try:
            row = ledger.conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(units),0), COALESCE(SUM(tokens),0) "
                "FROM api_calls WHERE provider=?", (prov,)).fetchone()
            d_calls = row[0] - pre[prov][0]
            d_units = row[1] - pre[prov][1]
            d_tok = row[2] - pre[prov][2]
            if d_calls or d_units or d_tok:
                P(f"  {prov:<14} {d_calls:>5} {d_units:>6} {d_tok:>8}")
        except Exception:                              # noqa: BLE001
            pass

    # ───── Verdict ─────────────────────────────────────────────────────────
    banner(P, "§7  Verdict")
    if result.get("failure"):
        P("  ✗ PIPELINE FAILED — LLM gave up. Review §3 + §4 fallback_errors.")
    elif result.get("parse_tier") == "failed":
        P("  ⚠ LLM RESPONDED BUT UNPARSEABLE — see raw_excerpt in §4.")
    elif result.get("parse_tier") == "regex_repair":
        P("  ⚠ LLM responded but didn't follow strict JSON instruction.")
        P("    Output usable via regex repair. Consider prompt tightening.")
    elif (result.get("home_goal_delta") == 0 and
          result.get("away_goal_delta") == 0):
        P("  ℹ All-zero deltas — either no usable signals were found, or")
        P("    the LLM legitimately judged there's nothing material to tilt.")
    else:
        P(f"  ✓ Pipeline produced a real signal: home {result.get('home_goal_delta'):+.2f},")
        P(f"    away {result.get('away_goal_delta'):+.2f}, confidence "
          f"{result.get('confidence')!r}")
    P()
    if report_path:
        P(f"  Full trace saved to: {report_path}")

    if fh:
        fh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
