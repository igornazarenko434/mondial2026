"""Show the LLM news-agent's per-card reasoning trail for any historical match.

Reads from `predictions.payload_json` — **zero API cost**. For a live
re-dive that spends fresh Brave + LLM calls, use `tools/news_inspect.py`.

What each card stores (Day-9.25/9.26/9.28 stamping):
  * news_provider          — which LLM answered (gemini / claude / openai)
  * news_raw_home_delta    — LLM's raw ±goals output for home BEFORE clamp/scale
  * news_raw_away_delta    — same for away
  * news_deltas            — final applied (raw × NEWS_CONFIDENCE_SCALE × ±0.15)
  * news_confidence        — LLM's self-report (low/medium/high)
  * news_notes             — the LLM's rationale, one bullet per finding
  * news_context_sources_ok — which sub-sources fed context (brave/lineups/injuries)
  * news_context_chars     — total context size sent to LLM
  * news_brave_gate        — why Brave was/wasn't called
  * news_fallbacks_used    — providers tried before one answered
  * news_parse_tier        — strict / regex_repair / empty / failed
  * news_raw_excerpt       — first 200 chars of raw LLM output (parse-fail only)
  * news_ctx_failures      — per-source errors

Usage:
  tools/news_reasoning.py <match_id>                   # dump all 4 windows
  tools/news_reasoning.py <match_id> --window T-60m    # one window
  tools/news_reasoning.py --teams "Argentina" "Switzerland"
  tools/news_reasoning.py --rank                       # find cards with the
                                                        #  richest per-team
                                                        #  reasoning
"""
from __future__ import annotations
import argparse
import json
import os
import sqlite3
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = "store/mondial.db"


def _wrap(text: str, indent: str = "    • ", cont: str = "      ",
           width: int = 88) -> list[str]:
    return textwrap.wrap(str(text), width=width,
                          initial_indent=indent, subsequent_indent=cont)


def _fetch_match(conn: sqlite3.Connection, match_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT match_id, home, away, stage, status, home_goals, away_goals, "
        "penalty_home, penalty_away, utc_kickoff "
        "FROM matches WHERE match_id = ?", (match_id,)
    ).fetchone()


def _match_by_teams(conn: sqlite3.Connection, home: str, away: str
                    ) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT match_id, home, away, stage, status, home_goals, away_goals, "
        "penalty_home, penalty_away, utc_kickoff "
        "FROM matches WHERE home = ? AND away = ?", (home, away)
    ).fetchone()
    if row:
        return row
    # Fallback — normalize via teams.normalize in case caller passed variants.
    from core.data.teams import normalize
    nh, na = normalize(home), normalize(away)
    return conn.execute(
        "SELECT match_id, home, away, stage, status, home_goals, away_goals, "
        "penalty_home, penalty_away, utc_kickoff "
        "FROM matches WHERE home = ? AND away = ?", (nh, na)
    ).fetchone()


def _dump_card(row: sqlite3.Row) -> None:
    """Pretty-print one card's news trail (one window)."""
    p = json.loads(row["payload_json"])
    window = row["window"]
    print("=" * 78)
    print(f"  {window}   created_at={row['created_at']}")
    print("=" * 78)
    provider = p.get("news_provider")
    fallbacks = p.get("news_fallbacks_used") or []
    prov_str = f"{provider}"
    if fallbacks:
        prov_str += f"  (fell back from: {fallbacks})"
    elif provider is None:
        prov_str += "   (no LLM call — reused prior window's deltas)"
    print(f"  provider:       {prov_str}")
    print(f"  raw δh / δa:    {p.get('news_raw_home_delta')} / "
          f"{p.get('news_raw_away_delta')}")
    print(f"  clamped δh/δa:  {p.get('news_deltas')}")
    print(f"  confidence:     {p.get('news_confidence')}")
    print(f"  ctx sources:    {p.get('news_context_sources_ok')}")
    ctx_chars = p.get("news_context_chars") or 0
    ctx_trunc = p.get("news_context_truncated_chars") or 0
    print(f"  ctx chars:      {ctx_chars}  (truncated {ctx_trunc})")
    print(f"  brave gate:     {p.get('news_brave_gate')}")
    print(f"  parse tier:     {p.get('news_parse_tier')}")
    ctx_fail = p.get("news_ctx_failures") or []
    if ctx_fail:
        print(f"  ctx failures:   {ctx_fail}")

    notes = p.get("news_notes") or []
    if notes:
        print(f"  LLM NOTES ({len(notes)}) — the LLM's own words:")
        for n in notes:
            for line in _wrap(n):
                print(line)
    else:
        print("  (no notes stored — either no signal found, or upstream failure)")

    ex = p.get("news_raw_excerpt")
    if ex:
        print("  raw_excerpt (present only when parse tier=failed):")
        for line in _wrap(ex, indent="    ", cont="    "):
            print(line)
    print()


def _dump_all_windows(match_id: int, only_window: str | None = None) -> int:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    m = _fetch_match(conn, match_id)
    if not m:
        print(f"! match_id {match_id} not found in matches table")
        return 2
    pen = ""
    if m["penalty_home"] is not None:
        pen = f"  (pens {m['penalty_home']}-{m['penalty_away']})"
    hg = m["home_goals"]
    ag = m["away_goals"]
    result = (f"FT {hg}-{ag}" if m["status"] == "FINISHED"
              else f"[{m['status']}]")
    print(f"\nMATCH {match_id}: {m['home']} vs {m['away']}  ({m['stage']})  "
          f"kickoff={m['utc_kickoff']}\n  {result}{pen}\n")
    sql = ("SELECT match_id, window, created_at, payload_json "
           "FROM predictions WHERE match_id = ?")
    params: tuple = (match_id,)
    if only_window:
        sql += " AND window = ?"
        params += (only_window,)
    sql += " ORDER BY created_at"
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print(f"! no predictions rows for match_id={match_id}")
        return 2
    for r in rows:
        _dump_card(r)
    return 0


def _rank_rich_cards(limit: int = 12) -> int:
    """Find historical T-60m cards with the richest per-team reasoning.

    Ranking signal (a proxy — "how much did the news agent actually chew"):
      * both raw_home_delta AND raw_away_delta non-zero  (both teams got signal)
      * many notes                                        (multiple findings)
      * larger context_chars                              (more input material)
      * brave_search + api_football sources all up        (full source stack)
    """
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    print("Ranking T-60m cards with rich per-team news reasoning "
          "(both teams had non-zero signal + multiple notes):\n")

    rows = conn.execute(
        "SELECT p.match_id, p.window, p.created_at, p.payload_json, "
        "       m.home, m.away, m.stage "
        "FROM predictions p JOIN matches m ON p.match_id = m.match_id "
        "WHERE p.window = 'T-60m'"
    ).fetchall()

    scored = []
    for r in rows:
        try:
            p = json.loads(r["payload_json"])
        except Exception:                                  # noqa: BLE001
            continue
        rh = p.get("news_raw_home_delta") or 0.0
        ra = p.get("news_raw_away_delta") or 0.0
        notes = p.get("news_notes") or []
        ctx_chars = p.get("news_context_chars") or 0
        srcs = p.get("news_context_sources_ok") or []
        has_brave = any("brave" in s for s in srcs)
        has_af = any("api_football" in s for s in srcs)
        both_teams = abs(rh) > 0.001 and abs(ra) > 0.001
        # Weighted score.
        score = (
            (30 if both_teams else 0)
            + 4 * len(notes)
            + min(ctx_chars, 8000) / 400
            + (5 if has_brave else 0)
            + (5 if has_af else 0)
        )
        scored.append({
            "match_id": r["match_id"], "home": r["home"], "away": r["away"],
            "stage": r["stage"], "score": score, "n_notes": len(notes),
            "raw_h": rh, "raw_a": ra, "ctx_chars": ctx_chars,
            "srcs": srcs, "provider": p.get("news_provider"),
        })

    scored.sort(key=lambda x: x["score"], reverse=True)

    print(f"{'score':>5}  {'match_id':<8} {'stage':<6} "
          f"{'home vs away':<45}  {'notes':>5} {'raw δh':>7} "
          f"{'raw δa':>7} {'ctx':>5} {'provider':<8}")
    print("-" * 108)
    for s in scored[:limit]:
        pair = f"{s['home']} vs {s['away']}"
        print(f"{s['score']:>5.1f}  {s['match_id']:<8} {s['stage']:<6} "
              f"{pair:<45}  {s['n_notes']:>5} {s['raw_h']:>+7.3f} "
              f"{s['raw_a']:>+7.3f} {s['ctx_chars']:>5} "
              f"{(s['provider'] or '-'):<8}")
    print()
    print("Tip: dump full reasoning for a match_id → "
          "`tools/news_reasoning.py <match_id>`")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="news_reasoning",
        description="Historical LLM news-agent reasoning trail per card. "
                    "Reads from predictions.payload_json; zero API cost.",
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("match_id", nargs="?", type=int, default=None,
                   help="football-data match_id (from matches.match_id)")
    g.add_argument("--teams", nargs=2, metavar=("HOME", "AWAY"),
                   help="Resolve match by team names instead of match_id")
    g.add_argument("--rank", action="store_true",
                   help="List T-60m cards with the richest per-team reasoning")
    p.add_argument("--window", choices=("T-24h", "T-60m", "T-15m", "T-7m"),
                    help="Only show one window")
    p.add_argument("--limit", type=int, default=12,
                    help="Ranking mode: how many rows to show (default 12)")
    args = p.parse_args(argv)

    if args.rank:
        return _rank_rich_cards(args.limit)

    if args.teams:
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        m = _match_by_teams(conn, *args.teams)
        if not m:
            print(f"! no match for teams {args.teams} — try match_id lookup")
            return 2
        return _dump_all_windows(m["match_id"], args.window)

    if args.match_id is None:
        p.print_help()
        return 2

    return _dump_all_windows(args.match_id, args.window)


if __name__ == "__main__":
    sys.exit(main())
