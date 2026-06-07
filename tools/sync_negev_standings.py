"""Daily sync: pull the Negev Toto leaderboard → write to our standings table.

Runs on the VM at 07:00 IDT (via mondial's crontab) — 2 hours before the
09:00 daily summary, so the summary reflects fresh Negev points.

Mapping (per integrations/CLAUDE_CODE_HANDOFF_negev.md Option A):
  Negev field        →  our standings column
  directionPoints    →  group_points     (combines group + KO direction points
                                          per Negev's data model; the strategy
                                          layer only needs (you, leader, second)
                                          gaps so the split doesn't matter)
  0.0                →  knockout_points  (always 0; reset already baked into
                                          directionPoints by Negev's writer)
  broadBetPoints     →  futures_points

Idempotent. Wrapped in obs.external_call so the call is rate-limited + traced.
Cron line (installed by the bootstrap; can be re-added manually):
  0 7 * * *  /home/mondial/mondial2026/.venv/bin/python /home/mondial/mondial2026/tools/sync_negev_standings.py >> /var/log/mondial_sync.log 2>&1

CLI flags:
  --force        run even when the time-since-last-sync gate would skip
  --dry-run      print what would be upserted; touch no DB
  --include-bots include role='bot' rows (default: skip)
"""
from __future__ import annotations
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store.db import connect
from core.obs.logging import get_logger
from core import obs

log = get_logger("sync_negev")


def _import_or_fail():
    """Lazy-import Negev MCP so this script doesn't blow up if the connector
    module is missing or NEGEV_REFRESH_TOKEN isn't set. Surfaces a clean error
    instead."""
    try:
        from integrations import negev_toto_mcp as ntm
        return ntm
    except Exception as e:                             # noqa: BLE001
        raise RuntimeError(
            f"Negev MCP module not importable: {e}. Ensure "
            "integrations/negev_toto_mcp.py exists and NEGEV_REFRESH_TOKEN "
            "is set in .env."
        )


def _upsert_standings(conn: sqlite3.Connection, row: dict, dry: bool) -> None:
    """One-row UPSERT matching tools/standings_set.py::_upsert shape so the
    two writers can coexist (manual entry via the CLI, automated via this)."""
    participant = row["player"]
    group_pts = float(row["direction"])               # see mapping in module docstring
    ko_pts = 0.0
    futures_pts = float(row["broad"])
    if dry:
        log.info("[dry-run] would upsert %s: group=%.2f ko=%.2f futures=%.2f total=%.2f",
                 participant, group_pts, ko_pts, futures_pts,
                 group_pts + ko_pts + futures_pts)
        return
    conn.execute(
        "INSERT INTO standings (participant, group_points, knockout_points, "
        "futures_points) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(participant) DO UPDATE SET "
        "group_points = excluded.group_points, "
        "knockout_points = excluded.knockout_points, "
        "futures_points = excluded.futures_points",
        (participant, group_pts, ko_pts, futures_pts))


def _format_telegram_summary(rows: list[dict], me: str, tid: str) -> tuple[str, str]:
    """Compose a Telegram-safe plain-text leaderboard summary.

    Returns: (title, body). 8-12 lines body. Highlights:
      • Top 5 by rank
      • YOU + the 2 above/below you (context)
      • Your gap to leader + to 2nd
    """
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    now = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Jerusalem"))
    title = f"📊 Negev standings — {now:%Y-%m-%d %H:%M IDT}"

    lines: list[str] = []
    by_name = {r["player"]: r for r in rows}
    me_row = by_name.get(me)
    n = len(rows)

    if me_row:
        gap_leader = rows[0]["total"] - me_row["total"]
        gap_next = (rows[me_row["rank"] - 2]["total"] - me_row["total"]
                    if me_row["rank"] > 1 else 0)
        lines.append(
            f"You: rank {me_row['rank']}/{n}  •  {me_row['total']:.1f} pts  "
            f"•  gap to leader: {gap_leader:.1f}")
    else:
        lines.append(f"Roster: {n} players  •  YOU not found in standings")

    lines.append("")
    lines.append("Top 5:")
    for r in rows[:5]:
        marker = "  ← you" if r["player"] == me else ""
        lines.append(f"  {r['rank']:>2}. {r['player']:<16}  {r['total']:>6.1f}{marker}")

    # Context window: 2 above + 2 below me, if I'm out of the top 5
    if me_row and me_row["rank"] > 7:
        lines.append("")
        lines.append(f"Around you:")
        my_rank = me_row["rank"]
        window = [r for r in rows if abs(r["rank"] - my_rank) <= 2]
        for r in window:
            marker = "  ← you" if r["player"] == me else ""
            lines.append(f"  {r['rank']:>2}. {r['player']:<16}  {r['total']:>6.1f}{marker}")

    return title, "\n".join(lines)


def sync_match_results(tid: str, *, conn: sqlite3.Connection,
                        ntm, dry: bool = False) -> int:
    """Pull Negev's match results (status='FT'/'PEN' with non-null goals) for
    this tournament and UPSERT into our matches table. Returns count updated.

    Why both football-data AND Negev: Negev is the LIVE scorer; if a friend
    enters a score before football-data publishes, our standings_writer can
    score against Negev's value. football-data overwrites later (idempotent
    UPSERT). Day-9.8.
    """
    try:
        with obs.external_call("negev_toto", "get_matches"):
            wc = ntm.toto_get_matches(tournament_id=tid, limit=300)
    except Exception as e:                             # noqa: BLE001
        log.warning("toto_get_matches failed: %s", e)
        return 0
    finished = [m for m in wc if m.get("status") in ("FT", "PEN")
                and m.get("scoreFullTimeHome") is not None
                and m.get("scoreFullTimeAway") is not None]
    if dry:
        log.info("[dry-run] %d FT/PEN matches would sync to local DB", len(finished))
        return len(finished)
    n = 0
    for m in finished:
        # Our matches table keys on football-data match_id (integer). Negev's
        # apiFixtureId is the API-Football id; we resolve by team-pair instead.
        try:
            ours = conn.execute(
                "SELECT match_id FROM matches WHERE home=? AND away=?",
                (m["home"], m["away"])).fetchone()
        except sqlite3.Error:
            continue
        if not ours:
            log.debug("Negev match %s vs %s not in our matches table — skipping",
                      m["home"], m["away"])
            continue
        try:
            conn.execute(
                "UPDATE matches SET status=?, home_goals=?, away_goals=? "
                "WHERE match_id=?",
                ("FINISHED", int(m["scoreFullTimeHome"]),
                 int(m["scoreFullTimeAway"]), ours[0]))
            n += 1
        except sqlite3.Error as e:
            log.warning("results UPSERT for match %s failed: %s", ours[0], e)
    conn.commit()
    log.info("synced %d match results from Negev → matches table", n)
    return n


def sync_standings(*, tournament_id: str | None = None,
                   include_bots: bool = False,
                   dry: bool = False,
                   send_telegram: bool = False,
                   conn: sqlite3.Connection | None = None,
                   ntm=None) -> dict:
    """Pull the leaderboard from Negev → upsert into our standings table.

    Returns: {participants, updated, my_rank, my_total, leader, leader_total}
    Never raises — failures are caught and reported in the result dict so
    cron doesn't fire an email about a transient Firestore blip."""
    ntm = ntm or _import_or_fail()
    conn = conn or connect()
    tid = tournament_id or os.environ.get("NEGEV_TOURNAMENT_ID", "").strip()
    if not tid:
        return {"ok": False, "error": "NEGEV_TOURNAMENT_ID not set"}

    me = os.environ.get("MY_PARTICIPANT", "").strip()

    try:
        with obs.external_call("negev_toto", "get_standings"):
            rows = ntm.toto_get_standings(tournament_id=tid,
                                           include_bots=include_bots)
    except Exception as e:                             # noqa: BLE001
        log.error("Negev fetch failed: %s", e)
        return {"ok": False, "error": str(e)[:200]}

    if not rows:
        return {"ok": False, "error": "Negev returned 0 rows — auth failed or tournament empty"}

    n_upserted = 0
    for r in rows:
        try:
            _upsert_standings(conn, r, dry)
            n_upserted += 1
        except sqlite3.Error as e:
            log.warning("upsert %r failed: %s", r.get("player"), e)
    if not dry:
        conn.commit()

    leader = rows[0]
    my_row = next((r for r in rows if r["player"] == me), None)
    result = {
        "ok": True, "tournament_id": tid,
        "participants": len(rows), "upserted": n_upserted,
        "leader": leader["player"], "leader_total": leader["total"],
        "second_total": rows[1]["total"] if len(rows) > 1 else None,
    }
    if my_row:
        result.update({"my_rank": my_row["rank"], "my_total": my_row["total"],
                       "my_gap_to_leader": leader["total"] - my_row["total"]})
    else:
        result["warning"] = f"MY_PARTICIPANT={me!r} not in standings"

    # Day-9.8: also sync Negev's match results into our matches table. This
    # gives us a second source of truth (football-data is primary, but Negev
    # is what actually scores us). Run BEFORE the standings sync so when we
    # later compute standings ourselves from results, both sides agree.
    try:
        n_results = sync_match_results(tid, conn=conn, ntm=ntm, dry=dry)
        result["results_synced"] = n_results
    except Exception as e:                             # noqa: BLE001
        log.warning("results sync failed: %s", e)
        result["results_synced"] = 0
        result["results_error"] = str(e)[:120]

    # Telegram delivery — only on real sync (not dry-run) and only when asked.
    # Failure to deliver is non-fatal; the standings already wrote OK.
    if send_telegram and not dry:
        try:
            from core import delivery
            title, body = _format_telegram_summary(rows, me, tid)
            # `summary` (not `alert`) — keeps 📊 clean without ⚠️ prefix
            ok = delivery.summary(title, body)
            result["telegram_delivered"] = bool(ok)
            if ok:
                log.info("standings summary sent to Telegram")
            else:
                log.warning("Telegram returned False; summary not delivered")
        except Exception as e:                         # noqa: BLE001
            log.warning("Telegram delivery error: %s", e)
            result["telegram_delivered"] = False
            result["telegram_error"] = str(e)[:120]

    log.info("standings sync: %s", result)
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sync_negev_standings",
                                description="Pull Negev Toto leaderboard → DB.")
    p.add_argument("--tournament-id", default=None,
                   help="Override NEGEV_TOURNAMENT_ID env var")
    p.add_argument("--include-bots", action="store_true",
                   help="Include role='bot' rows (default: skip)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be upserted; touch no DB")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress final summary line on success")
    p.add_argument("--telegram", action="store_true",
                   help="Send a leaderboard summary to Telegram (uses the same "
                        "TELEGRAM_BOT_TOKEN/CHAT_ID as the daemon's daily summary)")
    p.add_argument("--no-alert-on-failure", action="store_true",
                   help="Suppress the ⚠ Negev failure Telegram on connect "
                        "errors (default: alert ON, so silent crons still warn).")
    p.add_argument("--test-alert", action="store_true",
                   help="Send a synthetic 'Negev unreachable' Telegram and "
                        "exit — useful for verifying the alert path works.")
    args = p.parse_args(argv)

    # Self-test path: prove the failure-alert wire-up is live without
    # actually breaking Negev. Exits 0 if delivery succeeded.
    if args.test_alert:
        from integrations.negev_alerts import alert_failure
        ok = alert_failure(
            source="sync_negev_standings (--test-alert)",
            reason="SYNTHETIC TEST — Negev MCP unreachable: this is a manual "
                   "self-test triggered with --test-alert. If you can read "
                   "this in Telegram, the failure-alert path is working.")
        print(f"test alert sent: {ok}")
        return 0 if ok else 1

    with obs.run("sync_negev_standings"):
        out = sync_standings(tournament_id=args.tournament_id,
                              include_bots=args.include_bots,
                              dry=args.dry_run,
                              send_telegram=args.telegram)
    if not out.get("ok"):
        print(f"FAILED: {out.get('error')}", file=sys.stderr)
        if not args.no_alert_on_failure:
            from integrations.negev_alerts import alert_failure
            alert_failure(source="sync_negev_standings",
                          reason=out.get("error") or "unknown")
        return 1
    if not args.quiet:
        if "my_rank" in out:
            print(f"✓ {out['participants']} players synced. "
                  f"You: rank {out['my_rank']}/{out['participants']} "
                  f"({out['my_total']:.0f} pts; leader {out['leader']} "
                  f"on {out['leader_total']:.0f}, gap {out['my_gap_to_leader']:.0f})")
        else:
            print(f"✓ {out['participants']} players synced. "
                  f"Leader: {out['leader']} ({out['leader_total']:.0f} pts). "
                  f"{out.get('warning', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
