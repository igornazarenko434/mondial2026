"""Show the live schedule state for upcoming matches.

For each match within --hours of NOW, prints:
  • utc_kickoff + local time
  • all 4 windows (T-24h / T-60m / T-15m / T-7m) with absolute fire time
    in both UTC and local
  • status of each window:  ⏳ pending  ⚙ due-now  ✓ fired  ⏭ skipped
  • countdown to the next window

Also prints daemon health:
  • Is mondial2026.service active?
  • When did it last beat (heartbeat freshness)?
  • Catch-up cap (120 min by default — late jobs only fire if within this)

Read-only. Costs 0 API credits.

  PYTHONPATH=. .venv/bin/python tools/show_schedule.py
  PYTHONPATH=. .venv/bin/python tools/show_schedule.py --hours 48
  PYTHONPATH=. .venv/bin/python tools/show_schedule.py --match "Mexico"
"""
from __future__ import annotations
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fmt_local(dt: datetime, tz_name: str = "Asia/Jerusalem") -> str:
    from zoneinfo import ZoneInfo
    return dt.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M %Z")


def _countdown(target: datetime, now: datetime) -> str:
    delta = target - now
    secs = int(delta.total_seconds())
    if secs < 0:
        secs = -secs
        sign = "ago"
    else:
        sign = "from now"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _s = divmod(rem, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if h or d: parts.append(f"{h:02d}h")
    parts.append(f"{m:02d}m")
    return f"{' '.join(parts)} {sign}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="show_schedule")
    p.add_argument("--hours", type=int, default=48,
                   help="Show matches within next N hours (default 48)")
    p.add_argument("--match", default=None,
                   help="Filter to matches whose home or away contains this substring")
    p.add_argument("--catchup-min", type=int, default=120,
                   help="Match the daemon's catch-up cap (default 120)")
    args = p.parse_args(argv)

    from schedule.scheduler import WINDOWS, jobs_for_match
    from store.db import connect
    from core.obs.runs import runs

    now = datetime.now(timezone.utc)
    cutoff = (now + timedelta(hours=args.hours)).isoformat()

    print()
    print(f"  ╔════════════════════════════════════════════════════════════╗")
    print(f"  ║  Schedule state — {now.isoformat(timespec='seconds')}")
    print(f"  ║  Local: {_fmt_local(now)}")
    print(f"  ╚════════════════════════════════════════════════════════════╝")

    # ──── Daemon health ────
    print()
    print(f"  ── Daemon health ──")
    import subprocess
    try:
        active = subprocess.run(["systemctl", "is-active", "mondial2026"],
                                  capture_output=True, text=True, timeout=5)
        print(f"  mondial2026.service: {active.stdout.strip()}")
    except Exception:                                       # noqa: BLE001
        print(f"  mondial2026.service: (systemctl not available — running locally?)")

    # Heartbeat freshness
    hb_path = os.environ.get("HEARTBEAT_FILE", "store/heartbeat")
    try:
        hb_mtime = os.path.getmtime(hb_path)
        hb_age = now.timestamp() - hb_mtime
        flag = "✓" if hb_age < 180 else "⚠ STALE"
        print(f"  Heartbeat:           {hb_age:.0f}s ago  {flag}")
    except OSError:
        print(f"  Heartbeat:           (file not found — daemon may not have ticked yet)")
    print(f"  Catch-up window:     {args.catchup_min} min "
          f"(late jobs only fire within this)")

    # ──── Upcoming matches ────
    conn = connect()
    rows = conn.execute(
        "SELECT match_id, utc_kickoff, stage, grp, home, away, status, "
        "detonator FROM matches "
        "WHERE utc_kickoff >= ? AND utc_kickoff <= ? "
        "AND status IN ('SCHEDULED', 'TIMED') "
        "ORDER BY utc_kickoff",
        (now.isoformat(), cutoff)).fetchall()

    if args.match:
        q = args.match.lower()
        rows = [r for r in rows
                 if q in r["home"].lower() or q in r["away"].lower()]

    if not rows:
        print(f"\n  No upcoming matches in the next {args.hours}h.")
        return 0

    print()
    print(f"  ── Matches in next {args.hours}h ({len(rows)}) ──")
    led = runs()
    for r in rows:
        ko = datetime.fromisoformat(r["utc_kickoff"])
        if ko.tzinfo is None:
            ko = ko.replace(tzinfo=timezone.utc)
        det = "  ⚡ DETONATOR" if r["detonator"] else ""
        print()
        print(f"  ⚽ {r['home']} vs {r['away']}  "
              f"({r['stage']}{(' ' + r['grp']) if r['grp'] else ''}){det}")
        print(f"    Kickoff (UTC):   {ko.isoformat(timespec='minutes')}")
        print(f"    Kickoff (local): {_fmt_local(ko)}")
        print(f"    Match ID:        {r['match_id']}")
        print()
        print(f"    {'Window':<7}  {'Fire time (UTC)':<22} {'Local':<28} "
              f"{'Status':<14} Countdown")
        print(f"    {'-'*7}  {'-'*22} {'-'*28} {'-'*14} {'-'*18}")
        for w, delta in WINDOWS.items():
            fire = ko - delta
            late = (now - fire).total_seconds()
            done = led.was_handled(r["match_id"], w)
            if done:
                status = "✓ fired"
            elif late < 0:
                status = "⏳ pending"
            elif late <= args.catchup_min * 60:
                status = "⚙ DUE NOW"
            else:
                status = "⏭ skipped"      # past catch-up cap, won't fire
            print(f"    {w:<7}  {fire.isoformat(timespec='minutes'):<22} "
                  f"{_fmt_local(fire):<28} {status:<14} "
                  f"{_countdown(fire, now)}")

    # Next window summary across all matches
    print()
    next_target = None
    next_label = None
    for r in rows:
        ko = datetime.fromisoformat(r["utc_kickoff"])
        if ko.tzinfo is None:
            ko = ko.replace(tzinfo=timezone.utc)
        for w, delta in WINDOWS.items():
            fire = ko - delta
            if fire > now and not led.was_handled(r["match_id"], w):
                if next_target is None or fire < next_target:
                    next_target = fire
                    next_label = f"{w} for {r['home']} vs {r['away']}"
    if next_target:
        print(f"  ▶ Next window to fire: {next_label}")
        print(f"    at {_fmt_local(next_target)}  "
              f"({_countdown(next_target, now)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
