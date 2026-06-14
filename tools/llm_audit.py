"""LLM agent observability audit (Day-9.10).

Reads the persisted cost ledger (`store/mondial.db::api_calls`, same DB as
game data since Day-9.28 DB merge) and the `predictions` table to answer
the questions you actually have at 2am when a card didn't fire:

  • Which LLM provider actually answered each match-window?
  • How many times did Gemini fail vs Claude vs OpenAI — and with what
    error class (RateLimitError / AuthenticationError / APITimeoutError /
    APIConnectionError / etc.)?
  • Was the LLM's output unparseable? Which parse tier landed
    (strict / regex_repair / failed)?  If failed, what did the LLM say?
  • Are any providers currently over budget?
  • What's the total spend so far (real $ + free-tier usage %)?

Run on the VM:
    sudo -u mondial bash -c '
      cd /home/mondial/mondial2026
      set -a && source .env && set +a
      PYTHONPATH=. .venv/bin/python tools/llm_audit.py
    '

Filter by hours: `--hours 24` (default 168 = 1 week)
Filter by provider: `--provider gemini`
"""
from __future__ import annotations
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import observability as cfg
from core.obs.cost import ledger
from store.db import connect

LLM_PROVIDERS = ("gemini", "claude", "openai")


def banner(title: str) -> None:
    print(f"\n{'=' * 70}\n  {title}\n{'=' * 70}")


def show_chain_state() -> None:
    """Which providers are configured + which are currently bypassed and why."""
    from core.llm.router import LLMRouter
    banner("1. LLM CHAIN STATE — what would run RIGHT NOW for a fresh card")
    r = LLMRouter()
    print(f"  Chain (config): {r.chain}")
    avail = r._ordered_available()
    avail_names = [p.name for p in avail]
    print(f"  Available now:  {avail_names}")
    print()
    print(f"  Bypass reasons (would be skipped this call):")
    L = ledger()
    for name in r.chain:
        p = r.registry.get(name)
        if not p:
            print(f"    {name:<10} : not in registry")
            continue
        if not p.available():
            print(f"    {name:<10} : NO KEY  (skip — env var not set)")
            continue
        if L.over_budget(name):
            st = L.quota_status(name)
            print(f"    {name:<10} : OVER BUDGET  "
                  f"({st['used']}/{st['budget']} this {st['period']})")
            continue
        print(f"    {name:<10} : ✓ available")


def show_per_provider_ledger(hours: int, provider_filter: str | None) -> None:
    """Per-provider call counts, success vs failure, by error class."""
    banner(f"2. PER-PROVIDER LEDGER — last {hours}h"
            + (f" (provider={provider_filter})" if provider_filter else ""))
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    L = ledger()
    providers = [provider_filter] if provider_filter else LLM_PROVIDERS

    for p in providers:
        rows = L.conn.execute(
            # units > 0: each LLM call produces 2 rows (call=units=1, token-update=units=0).
            # Filtering units > 0 counts real calls only (not the token-update rows).
            "SELECT ok, error_class, error_message, COUNT(*) AS n, "
            "       COALESCE(SUM(tokens), 0) AS toks, "
            "       COALESCE(AVG(duration_ms), 0) AS avg_ms, "
            "       COALESCE(SUM(est_cost), 0) AS cost "
            "  FROM api_calls "
            " WHERE provider=? AND ts>=? AND units > 0 "
            " GROUP BY ok, error_class "
            " ORDER BY ok DESC, n DESC",
            (p, since)).fetchall()
        total = sum(r[3] for r in rows)
        if not total:
            print(f"\n  {p:<10}  (no calls in window)")
            continue
        ok = sum(r[3] for r in rows if r[0] == 1)
        fail = total - ok
        toks = sum(r[4] for r in rows)
        avg_ms = (sum(r[5] * r[3] for r in rows) / total) if total else 0
        cost = sum(r[6] for r in rows)
        print(f"\n  {p:<10}  calls={total}  ok={ok}  fail={fail}  "
              f"tokens≈{toks}  avg={avg_ms:.0f}ms  est=${cost:.4f}")
        if fail:
            print(f"    failures by class:")
            for r in rows:
                if r[0] == 1:
                    continue
                ec = r[1] or "(unspecified)"
                em = (r[2] or "")[:80]
                print(f"      • {ec:<28} ×{r[3]}  e.g. {em!r}")


def show_quota_state() -> None:
    """Budget vs used per provider. Same logic the over-budget short-circuit
    uses — so this view is authoritative."""
    banner("3. QUOTA STATE — budget vs. used")
    L = ledger()
    for p in cfg.PROVIDER_LIMITS:
        st = L.quota_status(p)
        if not st.get("budget"):
            print(f"  {p:<14}  no metered budget")
            continue
        used = st.get("used", 0)
        budget = st["budget"]
        frac = st.get("fraction", 0.0)
        warn = " ⚠" if st.get("warn") else ""
        over = " 🛑 OVER" if used >= budget else ""
        print(f"  {p:<14}  {used:>5}/{budget:<5} ({frac*100:>5.1f}%) "
              f"period={st.get('period', '?')}{warn}{over}")


def show_news_card_audit(hours: int) -> None:
    """For each recent card in `predictions`, show news_provider + parse_tier
    + raw_excerpt (if parse failed). This is the per-match story —
    'why is the news signal showing 0.0 deltas for Mexico vs SA?'"""
    banner(f"4. NEWS CARD AUDIT — last {hours}h of predictions")
    try:
        conn = connect()
    except Exception as e:                              # noqa: BLE001
        print(f"  (mondial.db unreadable: {e})")
        return
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        rows = conn.execute(
            "SELECT match_id, window, created_at, payload_json "
            "  FROM predictions "
            " WHERE created_at >= ? "
            " ORDER BY created_at DESC "
            " LIMIT 20",
            (since,)).fetchall()
    except sqlite3.OperationalError as e:
        print(f"  (predictions table missing or schema mismatch: {e})")
        return
    if not rows:
        print("  (no predictions in window — pre-tournament idle state is OK)")
        return
    print(f"  {'when':<20} {'match_id':<10} {'win':<5} {'provider':<10} "
          f"{'parse':<14} {'fallbacks':<22} {'fail?':<28}")
    print("  " + "-" * 110)
    for r in rows:
        try:
            c = json.loads(r["payload_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            c = {}
        prov = c.get("news_provider") or "-"
        tier = c.get("news_parse_tier") or "-"
        fb = ",".join(c.get("news_fallbacks_used") or []) or "-"
        fail = c.get("news_failure") or c.get("news_failure_class") or "-"
        when = (r["created_at"] or "")[:19]
        print(f"  {when:<20} {r['match_id']!s:<10} {r['window']:<5} "
              f"{prov:<10} {tier:<14} {fb:<22} {fail[:26]:<28}")
        # If parse failed AND we captured the raw excerpt, show it.
        excerpt = c.get("news_raw_excerpt")
        if excerpt:
            print(f"     ↳ raw LLM output[:200]: {excerpt!r}")
        # If we have classified upstream errors, surface them.
        per_provider = c.get("news_fallback_errors") or {}
        if per_provider:
            for pname, err in per_provider.items():
                ec = (err or {}).get("error_class", "?")
                em = (err or {}).get("error_message", "")[:60]
                print(f"     ↳ {pname} failed: {ec}  {em!r}")


def show_news_deltas_dashboard(hours: int) -> None:
    """Day-9.23: per-card news_deltas + confidence visual. Catches the
    'news always returns 0.0/0.0 NEUTRAL' degradation — if every card
    shows 0/0 with confidence=low, the news agent is silently failing
    even when parse_tier=strict."""
    banner(f"5. NEWS DELTAS DASHBOARD — last {hours}h (catches silent-NEUTRAL drift)")
    try:
        conn = connect()
    except Exception as e:                              # noqa: BLE001
        print(f"  (mondial.db unreadable: {e})")
        return
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        rows = conn.execute(
            "SELECT match_id, window, created_at, payload_json FROM predictions "
            "WHERE created_at >= ? ORDER BY created_at DESC LIMIT 30",
            (since,)).fetchall()
    except sqlite3.OperationalError as e:
        print(f"  (predictions table missing: {e})")
        return
    if not rows:
        print("  (no predictions in window)")
        return
    print(f"  {'when':<20} {'match':<8} {'win':<5} {'home_δ':<10} "
          f"{'away_δ':<10} {'conf':<8} {'clamped?':<10} {'provider':<10}")
    print("  " + "-" * 90)
    neutral_count = 0
    nonzero_count = 0
    for r in rows:
        try:
            c = json.loads(r["payload_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            c = {}
        hd = c.get("news_home_delta", 0)
        ad = c.get("news_away_delta", 0)
        conf = c.get("news_confidence", "?")
        clamped = "✓" if (c.get("news_home_delta_clamped")
                           or c.get("news_away_delta_clamped")) else "-"
        prov = c.get("news_provider") or "-"
        when = (r["created_at"] or "")[:19]
        if abs(hd) < 0.001 and abs(ad) < 0.001:
            neutral_count += 1
        else:
            nonzero_count += 1
        # Visual delta bar (-0.6 to +0.6 → 12 chars)
        def bar(d):
            if not isinstance(d, (int, float)):
                return "?"
            pos = int(round(d * 10))
            return f"{d:+.2f}"
        print(f"  {when:<20} {r['match_id']!s:<8} {r['window']:<5} "
              f"{bar(hd):<10} {bar(ad):<10} {conf:<8} {clamped:<10} {prov:<10}")
    print()
    total = neutral_count + nonzero_count
    if total:
        pct_neutral = neutral_count / total * 100
        flag = ""
        if pct_neutral > 70:
            flag = "  🛑 HIGH — news_agent likely failing silently"
        elif pct_neutral > 40:
            flag = "  ⚠ elevated"
        print(f"  Neutral (0/0) deltas: {neutral_count}/{total}  "
              f"({pct_neutral:.0f}%){flag}")
        print(f"  Non-zero deltas:      {nonzero_count}/{total}")


def show_recent_failures(hours: int, limit: int = 10) -> None:
    """Latest LLM failures across all providers — with timestamp, provider,
    error class, and message. Useful for 'what just broke?'"""
    banner(f"5. RECENT LLM FAILURES — last {hours}h (up to {limit} most recent)")
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    L = ledger()
    rows = L.conn.execute(
        "SELECT ts, provider, endpoint, error_class, error_message, "
        "       duration_ms, correlation_id "
        "  FROM api_calls "
        " WHERE ok=0 AND provider IN ({}) AND ts>=? "
        " ORDER BY ts DESC LIMIT ?".format(
            ",".join("?" * len(LLM_PROVIDERS))),
        (*LLM_PROVIDERS, since, limit)).fetchall()
    if not rows:
        print("  ✓ No LLM failures in window.")
        return
    for r in rows:
        ts, prov, ep, ec, em, dur, cid = r
        print(f"  • {ts[:19]}  {prov:<8}/{ep:<10}  {ec or '?'}  "
              f"({dur:.0f}ms)  cid={cid}")
        if em:
            print(f"      {em[:160]!r}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="llm_audit",
                                description="LLM agent observability audit.")
    p.add_argument("--hours", type=int, default=168,
                   help="Window in hours (default 168 = 1 week)")
    p.add_argument("--provider", choices=LLM_PROVIDERS,
                   help="Filter section 2 to one provider")
    args = p.parse_args(argv)

    print(f"\n  LLM observability audit — window={args.hours}h  "
          f"now={datetime.now(timezone.utc).isoformat()}")

    show_chain_state()
    show_per_provider_ledger(args.hours, args.provider)
    show_quota_state()
    show_news_card_audit(args.hours)
    show_news_deltas_dashboard(args.hours)            # Day-9.23: §F
    show_recent_failures(args.hours)

    print("\n  ✓ Done. Each section above answers ONE of the questions on the\n"
          "    runbook. Cross-check: section 1 explains TODAY's bypass, section 2\n"
          "    explains LAST WEEK's misses by class, section 4 ties each card\n"
          "    back to its model + parse tier, section 5 lists raw failures.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
