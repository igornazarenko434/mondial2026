"""Day-9.22: fire ONE of every Telegram message type at the channel so the
operator can visually verify formatting after a configuration change.

Each subcommand uses the REAL production code path (delivery.summary +
people renderer) — so what lands in the channel is byte-identical to what
the daemon will send in production.

Usage (on the VM):
    sudo -u mondial bash -c '
      cd /home/mondial/mondial2026
      set -a && source .env && set +a
      PYTHONPATH=. .venv/bin/python tools/smoke_test_messages.py all
    '

Subcommands:
    standings     fire one 📊 (calls real Negev, renders all tracked blocks)
    daily         fire one ☀️ daily summary (today's games + tracked blocks)
    kickoff       fire one ⚽ kickoff card (synthetic match; tests rendering only)
    card          fire one 🃏 match card (synthetic match; legacy + friends footer)
    all           fire ALL FOUR back-to-back

Each successful send returns 0; an HTTP/Telegram failure returns non-zero
and prints the error. Costs a handful of API credits (Negev + Telegram);
all well under free tiers.
"""
from __future__ import annotations
import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def fire_standings() -> bool:
    """Re-uses tools/sync_negev_standings.py — no DB write, just delivery."""
    from tools.sync_negev_standings import sync_standings
    out = sync_standings(send_telegram=True, dry=True)
    # dry=True skips DB write but we still want Telegram. Re-call without dry.
    out = sync_standings(send_telegram=True, dry=False)
    if not out.get("ok"):
        print(f"  ✗ standings failed: {out.get('error')}", file=sys.stderr)
        return False
    print(f"  ✓ standings sent ({out['participants']} players, "
          f"leader {out['leader']!r})")
    return bool(out.get("telegram_delivered", True))


def fire_daily() -> bool:
    """Re-uses schedule/daily_summary.build_summary_text + delivery.summary."""
    from store.db import connect, init_db
    from schedule.daily_summary import build_summary_text
    from core import delivery
    init_db()
    conn = connect()
    now = datetime.now(timezone.utc)
    body = build_summary_text(conn, now)
    today = now.astimezone().date().isoformat()
    ok = delivery.summary(f"☀️ Daily summary — {today}  (smoke test)", body)
    print(f"  {'✓' if ok else '✗'} daily summary sent")
    return bool(ok)


def fire_kickoff() -> bool:
    """Synthetic match → exercises kickoff_cards.build_kickoff_text +
    delivery.summary. Doesn't write to runs ledger (so it can be re-run)."""
    from schedule.kickoff_cards import build_kickoff_text
    from core import delivery
    from integrations import negev_toto_mcp as ntm
    # Probe match: the actual opener
    match = {
        "match_id": 1489369,
        "utc_kickoff": "2026-06-11T19:00:00+00:00",
        "stage": "Group", "group": "A",
        "home": "Mexico", "away": "South Africa",
    }
    try:
        details = ntm.toto_get_match_details(home="Mexico", away="South Africa")
        picks = details.get("friendsPicks") or []
        my_pred = details.get("myPrediction")
    except Exception as e:                              # noqa: BLE001
        print(f"  ⚠ Negev picks unavailable ({e}); using synthetic")
        picks, my_pred = [], None
    try:
        standings = ntm.toto_get_standings(include_bots=True)
    except Exception as e:                              # noqa: BLE001
        print(f"  ⚠ Negev standings unavailable ({e}); empty")
        standings = []
    # Lineups: probably not yet posted; degrade silently
    lineups = None
    title, body = build_kickoff_text(match, datetime.now(timezone.utc),
                                       picks, my_pred, standings, lineups)
    ok = delivery.summary(title + "  (smoke test)", body)
    print(f"  {'✓' if ok else '✗'} kickoff card sent")
    return bool(ok)


def fire_card() -> bool:
    """Synthetic 🃏 card → exercises render_card + the new picks footer."""
    from core.delivery.base import render_card
    from core.decision.build_card import _build_friend_picks_section
    from core import delivery
    section = _build_friend_picks_section("Mexico", "South Africa")
    card = {
        "home": "Mexico", "away": "South Africa", "stage": "Group", "group": "A",
        "kickoff_local": "2026-06-11 22:00",
        "detonator": True,
        "locked_odds": {"H": 1.85, "D": 3.60, "A": 4.20},
        "model_prob":  {"H": .67, "D": .21, "A": .12},
        "pick_direction": "H",
        "pick_exact_score": {"home": 2, "away": 1},
        "modal_score":      {"home": 2, "away": 1},
        "expected_points": 3.42,
        "signals_used":   ["dixon_coles", "elo", "market", "news"],
        "signals_failed": [],
        "failure_reasons": {},
        "news_provider": "gemini",
        "ev_pathway": "ev",
        "window": "T-7m",
        "friend_picks_section": section,
    }
    body = render_card(card)
    title = f"{card['home']} vs {card['away']} — pick  (smoke test)"
    ok = delivery.summary(title, body)
    print(f"  {'✓' if ok else '✗'} match card sent  "
          f"(friend_picks_section={'YES' if section else 'NO'})")
    return bool(ok)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="smoke_test_messages")
    p.add_argument("kind", choices=["standings", "daily", "kickoff", "card", "all"])
    args = p.parse_args(argv)

    print()
    print(f"  Tracked: {os.environ.get('MY_PARTICIPANT', '?')!r} + "
          f"FRIEND_PARTICIPANTS={os.environ.get('FRIEND_PARTICIPANTS', '')!r}")
    print(f"  Channel: TELEGRAM_CHAT_ID={os.environ.get('TELEGRAM_CHAT_ID', '?')}")
    print()

    fns = {"standings": fire_standings, "daily": fire_daily,
            "kickoff": fire_kickoff, "card": fire_card}
    rc = 0
    if args.kind == "all":
        for name, fn in fns.items():
            print(f"  → firing {name} …")
            try:
                if not fn():
                    rc = 1
            except Exception as e:                       # noqa: BLE001
                print(f"  ✗ {name} crashed: {e}", file=sys.stderr)
                rc = 1
            print()
    else:
        try:
            ok = fns[args.kind]()
            rc = 0 if ok else 1
        except Exception as e:                           # noqa: BLE001
            print(f"  ✗ {args.kind} crashed: {e}", file=sys.stderr)
            rc = 1
    print(f"  {'✓ ALL OK' if rc == 0 else '✗ FAILURES (see stderr)'}")
    print()
    return rc


if __name__ == "__main__":
    sys.exit(main())
