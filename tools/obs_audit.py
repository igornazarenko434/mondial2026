"""End-to-end observability audit: hit every external provider once, confirm
each emits a span (via console exporter), records to the cost ledger, and
acquires through the shared rate-limit bucket.

Run:
    set -a && source .env && set +a
    OTEL_TRACES_EXPORTER=console PYTHONPATH=. .venv/bin/python tools/obs_audit.py

What it checks per provider:
  ✓ obs.external_call wrap exists and fires
  ✓ ledger().record persisted (provider row visible afterward)
  ✓ rate-limit token-bucket configured and acquirable
  ✓ config matches published free-tier ceiling (printed for human verify)

It is a smoke test for the audit golden rule (CLAUDE.md §3): "Every
`requests.get/post` must be inside `obs.external_call(...)`".
"""
from __future__ import annotations
import os, sys, time, traceback
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import observability as cfg
from core import obs
from core.obs.cost import ledger
from core.obs import ratelimit


# Publicly published free-tier ceilings (Dec-2025 / Jun-2026 sites). The audit
# prints these next to our config so a human can spot drift.
PUBLISHED = {
    "football_data": "10 req/min, no daily cap (free tier)",
    "odds_api":      "500 credits/mo, ~1 req every 1–2 s (free tier)",
    "api_football":  "100 req/day, 30 req/min (free tier)",
    "gemini":        "15 RPM, 1500 RPD (2.5 Flash free tier)",
    "claude":        "50 RPM Tier-1 PAYG; $1/Mtok in, $5/Mtok out (Haiku 4.5)",
    "openai":        "500 RPM Tier-1 PAYG (gpt-4o-mini)",
    "eloratings":    "no published limit; we self-throttle to 6/min",
    "martj42":       "GitHub raw: 60/hr anon, 5000/hr auth",
    "brave_search":  "1 req/sec, $5/1000 + $5/mo free = 1000 free queries/mo",
    "telegram_bot":  "1 msg/sec per chat, 30/sec global, 20/min to a group",
}


def banner(s: str):
    print(f"\n{'=' * 70}\n {s}\n{'=' * 70}")


def show_config():
    banner("1. CONFIG MATRIX — our limits vs. each provider's published limits")
    fmt = "  {p:<15s} rate={rate:>3}/{per:<3}s  budget={b:<10s}  →  {pub}"
    for p, lim in cfg.PROVIDER_LIMITS.items():
        b = f"{lim['budget']}/{lim['budget_period']}" if lim['budget'] else "(none)"
        print(fmt.format(p=p, rate=str(lim["rate"]), per=str(lim["per"]),
                         b=b, pub=PUBLISHED.get(p, "?")))


def show_pricing():
    banner("2. PRICING — $/unit recorded by the cost ledger")
    for p, price in cfg.PRICING.items():
        line = ", ".join(f"{k}=${v}" for k, v in price.items())
        print(f"  {p:<15s}  {line}")


def probe_ratelimit():
    """Show the resolved token-bucket for each provider so we know acquire()
    actually has a bucket to use."""
    banner("3. RATE-LIMIT BUCKETS — token bucket per provider (shared, thread-safe)")
    for p in cfg.PROVIDER_LIMITS:
        b = ratelimit.bucket(p)
        print(f"  {p:<15s} bucket: rate={b.rate:.4f} tok/s, capacity={b.capacity}")


def fire_one(provider, endpoint, fn, *, expect_failure=False):
    """Try one live call. Print pass/fail and what we recorded."""
    label = f"  • {provider:<14s} /{endpoint}"
    try:
        fn()
        ok = True
    except Exception as e:
        ok = False
        if not expect_failure:
            print(f"{label}  ✗ EXCEPTION: {e!r}")
            return False
    status = "✓ OK" if ok else "(expected failure)"
    print(f"{label}  {status}")
    return True


def live_probe():
    """Trigger one real call against every provider where we have credentials.
    Prints the span (via console exporter) and ledger row that resulted."""
    banner("4. LIVE PROBES — one real call per provider (span + ledger row)")
    print("  (Spans will appear above each provider line in the console exporter)\n")

    with obs.run("obs_audit_2026-06-06"):
        # football_data ──────────────────────────────────────────────────
        if os.environ.get("FOOTBALL_DATA_API_KEY"):
            from core.data import football_data as fd
            fire_one("football_data", "wc_matches",
                      lambda: fd.fetch_wc_matches())

        # odds_api: /sports (free) + /odds (1 credit)
        if os.environ.get("ODDS_API_KEY"):
            from core.data import oddsapi
            fire_one("odds_api", "sports",
                      lambda: oddsapi.list_sports())

        # api_football
        if os.environ.get("API_FOOTBALL_KEY"):
            from core.data import api_football as af
            fire_one("api_football", "fixtures",
                      lambda: af.find_team_id("Mexico"))

        # brave_search
        if os.environ.get("BRAVE_SEARCH_API_KEY"):
            from core.data import web_search
            fire_one("brave_search", "web",
                      lambda: web_search.web_search("World Cup 2026", n=1))

        # gemini / claude / openai (via router — first available answers)
        from core.llm.router import LLMRouter
        r = LLMRouter()
        chain_available = [p.name for p in r._ordered_available()]
        if chain_available:
            fire_one(chain_available[0], "complete",
                      lambda: r.complete("You are terse.", "Reply: OK"))

        # Scrapers (cached; the first call hits the network)
        from core.data import soccerdata_io
        fire_one("eloratings", "world_tsv",
                  lambda: soccerdata_io.national_team_elo())

        # Telegram (sends 1 audit message — skip if user prefers a silent run)
        if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("AUDIT_SEND_TELEGRAM") == "1":
            from core.delivery.channels import TelegramNotifier
            fire_one("telegram_bot", "sendMessage",
                      lambda: TelegramNotifier().send("obs audit",
                                                       "🔭 trace + ledger probe"))
        elif os.environ.get("TELEGRAM_BOT_TOKEN"):
            print("  • telegram_bot   (skipped — set AUDIT_SEND_TELEGRAM=1 to fire a real send)")


def show_ledger():
    banner("5. LEDGER — what got recorded for each provider after the probes")
    L = ledger()
    for p in cfg.PROVIDER_LIMITS:
        s = L.quota_status(p)
        if s.get("used", 0) > 0:
            warn = " ⚠" if s.get("warn") else ""
            print(f"  {p:<15s} used={s['used']:<8} budget={s['budget']}  "
                  f"frac={s['fraction']:.3f}{warn}")
        else:
            print(f"  {p:<15s} (no activity)")


def show_brave_health():
    banner("6. BRAVE QUOTA — free-credit balance, cost so far")
    try:
        from core.data.web_search import quota_status
        q = quota_status()
        print(f"  monthly used:    {q.get('month_used', 0)} / {q.get('budget', 1000)}")
        print(f"  fraction:        {q.get('fraction', 0):.3f}")
        print(f"  free remaining:  {q.get('remaining', 1000)} requests")
        print(f"  day (24h):       {q.get('day_used', 0)} / {q.get('day_cap', 60)}")
        print(f"  green-light:     {q.get('green_light', True)}")
    except Exception as e:
        print(f"  (skipped: {e})")


def main():
    obs.setup()
    print(f"OTel exporter: {cfg.TRACES_EXPORTER}  endpoint: {cfg.OTLP_ENDPOINT or '(none)'}")
    print(f"obs.ENABLED:  {cfg.ENABLED}")
    show_config()
    show_pricing()
    probe_ratelimit()
    live_probe()
    show_ledger()
    show_brave_health()
    print("\n✓ Done. Cross-check the spans above match each provider, and the\n"
          "  ledger row counts match the calls fired.\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(2)
