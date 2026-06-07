"""ONE-call-per-provider smoke test (Day-9.11).

For every API key we have configured, hit the smallest possible endpoint
ONCE and report ✓/✗/SKIP. Same path the real daemon uses (obs.external_call
wrapping → cost ledger row → OTel span) so a green run here means the
production wiring is healthy end-to-end.

Run on the VM:
    sudo -u mondial bash -c '
      cd /home/mondial/mondial2026
      set -a && source .env && set +a
      PYTHONPATH=. .venv/bin/python tools/api_smoke_test.py
    '

Cost per run (worst case — all providers configured):
  football_data : 1 free unit  (no daily cap)
  odds_api      : 0 credits    (/sports endpoint is free)
  api_football  : 1 / 100 day
  brave_search  : 1 / 1000 month
  gemini        : 1 / 1500 day
  claude        : ~$0.001 PAYG (depends on tier)
  openai        : ~$0.0001 PAYG (depends on tier)
  telegram_bot  : 0           (getMe is free)
  negev_toto    : 1 read       (Firestore — no metered cost)

Total: ≤ 7 metered calls, all within free tiers. Safe to run daily; cron-ready.
"""
from __future__ import annotations
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OK = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
SKIP = "\033[33m–\033[0m"


def _line(name: str, status: str, detail: str = "") -> None:
    print(f"  {status} {name:<14}  {detail}")


def _probe(name: str, fn) -> str:
    """Run one probe. Returns 'ok' / 'fail' / 'skip'. Always prints a
    result line; never raises."""
    t0 = time.monotonic()
    try:
        detail = fn() or ""
        dur = (time.monotonic() - t0) * 1000
        _line(name, OK, f"{dur:>5.0f}ms  {detail}")
        return "ok"
    except RuntimeError as e:
        # Convention: probes raise RuntimeError("X not set") when their env
        # var is missing — surface as SKIP not FAIL so the summary line
        # accurately distinguishes "configured + broken" from "not configured".
        if "not set" in str(e):
            _skip(name, str(e))
            return "skip"
        dur = (time.monotonic() - t0) * 1000
        _line(name, FAIL, f"{dur:>5.0f}ms  RuntimeError: {str(e)[:80]}")
        return "fail"
    except Exception as e:                              # noqa: BLE001
        dur = (time.monotonic() - t0) * 1000
        _line(name, FAIL, f"{dur:>5.0f}ms  {type(e).__name__}: {str(e)[:80]}")
        return "fail"


def _skip(name: str, reason: str) -> None:
    _line(name, SKIP, f"        {reason}")


# ──────────────────── individual probes ────────────────────

def _probe_football_data() -> str:
    if not os.environ.get("FOOTBALL_DATA_API_KEY"):
        raise RuntimeError("FOOTBALL_DATA_API_KEY not set")
    from core.data import football_data as fd
    rows = fd.fetch_wc_matches()
    return f"{len(rows)} matches in WC"


def _probe_odds_api() -> str:
    if not os.environ.get("ODDS_API_KEY"):
        raise RuntimeError("ODDS_API_KEY not set")
    from core.data import oddsapi
    sports = oddsapi.list_sports()           # /sports — free
    return f"{len(sports)} sports listed"


def _probe_api_football() -> str:
    if not os.environ.get("API_FOOTBALL_KEY"):
        raise RuntimeError("API_FOOTBALL_KEY not set")
    from core.data import api_football as af
    tid = af.find_team_id("Mexico")
    return f"Mexico team_id={tid}"


def _probe_brave() -> str:
    if not os.environ.get("BRAVE_SEARCH_API_KEY"):
        raise RuntimeError("BRAVE_SEARCH_API_KEY not set")
    from core.data import web_search
    results = web_search.web_search("World Cup 2026 schedule", n=1)
    return f"{len(results)} result(s)"


def _probe_gemini() -> str:
    if not os.environ.get("GEMINI_API_KEY"):
        raise RuntimeError("GEMINI_API_KEY not set")
    from core.llm.providers import GeminiProvider
    txt = GeminiProvider().complete("You are terse.", "Reply: OK", max_tokens=8)
    return f"reply={str(txt)[:40]!r}"


def _probe_claude() -> str:
    if not (os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")):
        raise RuntimeError("ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN not set")
    from core.llm.providers import ClaudeProvider
    txt = ClaudeProvider().complete("You are terse.", "Reply: OK", max_tokens=8)
    return f"reply={str(txt)[:40]!r}"


def _probe_openai() -> str:
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY not set")
    from core.llm.providers import OpenAIProvider
    txt = OpenAIProvider().complete("You are terse.", "Reply: OK", max_tokens=8)
    return f"reply={str(txt)[:40]!r}"


def _probe_telegram() -> str:
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    import requests
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(body.get("description", "telegram returned ok=false"))
    return f"bot=@{body['result'].get('username', '?')}"


def _probe_negev() -> str:
    if not os.environ.get("NEGEV_REFRESH_TOKEN"):
        raise RuntimeError("NEGEV_REFRESH_TOKEN not set")
    from integrations import negev_toto_mcp as ntm
    r = ntm.toto_ping()
    if "error" in r:
        raise RuntimeError(r["error"])
    return f"uid={r.get('uid', '?')[:8]}…  collections={len(r.get('collections', []))}"


# ──────────────────── main ────────────────────

PROBES = [
    ("football_data", _probe_football_data),
    ("odds_api",      _probe_odds_api),
    ("api_football",  _probe_api_football),
    ("brave_search",  _probe_brave),
    ("gemini",        _probe_gemini),
    ("claude",        _probe_claude),
    ("openai",        _probe_openai),
    ("telegram_bot",  _probe_telegram),
    ("negev_toto",    _probe_negev),
]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="api_smoke_test",
                                description="One real call per configured provider.")
    p.add_argument("--only", choices=[name for name, _ in PROBES],
                   help="Run only this one probe")
    p.add_argument("--no-llm", action="store_true",
                   help="Skip Gemini/Claude/OpenAI (no PAYG cost; useful "
                        "when checking cheap providers only)")
    args = p.parse_args(argv)

    print(f"\n  API smoke test — {len(PROBES)} configured probes\n")
    counts = {"ok": 0, "fail": 0, "skip": 0}
    for name, fn in PROBES:
        if args.only and args.only != name:
            continue
        if args.no_llm and name in ("gemini", "claude", "openai"):
            _skip(name, "skipped via --no-llm")
            counts["skip"] += 1
            continue
        result = _probe(name, fn)
        counts[result] += 1

    print(f"\n  Summary: {counts['ok']} ✓   {counts['fail']} ✗   "
          f"{counts['skip']} – skipped\n")
    return 0 if counts["fail"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
