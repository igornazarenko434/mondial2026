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


def _migrate_standings_schema(conn: sqlite3.Connection) -> None:
    """Day-9.26: idempotent ALTER for the new side_points column.

    Older DB instances (deployed before Day-9.26) have a 4-column standings
    table; the rewritten sync needs side_points to round out Negev's 4-way
    breakdown (Group / KO / Side / Futures). ALTER is no-op when the
    column is already present.
    """
    try:
        conn.execute("ALTER TABLE standings ADD COLUMN side_points REAL DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass         # column already exists — sqlite3 raises "duplicate column"


def _upsert_standings(conn: sqlite3.Connection, row: dict, dry: bool) -> None:
    """One-row UPSERT matching tools/standings_set.py::_upsert shape so the
    two writers can coexist (manual entry via the CLI, automated via this).

    Day-9.26 mapping (NEW — aligned with Negev's app columns):
      Negev `direction` (group-stage match points)    → group_points
      Negev `knockout`  (KO match points)             → knockout_points
      Negev `side`      (side-bet pts — limited;       → side_points
                          see toto_get_standings docstring)
      Negev `broad`     (futures, awarded at end)     → futures_points

    Pre-Day-9.26 this mapped `direction` → group_points and 0 → KO, because
    the OLD toto_get_standings read user-doc globals where group + KO were
    folded together. Now they're split correctly by stage at the source.
    """
    participant = row["player"]
    group_pts = float(row.get("direction") or 0)
    ko_pts = float(row.get("knockout") or 0)
    side_pts = float(row.get("side") or 0)
    futures_pts = float(row.get("broad") or 0)
    if dry:
        log.info("[dry-run] would upsert %s: group=%.2f ko=%.2f side=%.2f "
                  "futures=%.2f total=%.2f",
                 participant, group_pts, ko_pts, side_pts, futures_pts,
                 group_pts + ko_pts + side_pts + futures_pts)
        return
    # Migrate first run (idempotent)
    _migrate_standings_schema(conn)
    conn.execute(
        "INSERT INTO standings (participant, group_points, knockout_points, "
        "futures_points, side_points) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(participant) DO UPDATE SET "
        "group_points = excluded.group_points, "
        "knockout_points = excluded.knockout_points, "
        "futures_points = excluded.futures_points, "
        "side_points = excluded.side_points",
        (participant, group_pts, ko_pts, futures_pts, side_pts))


def _format_telegram_summary(rows: list[dict], me: str, tid: str) -> tuple[str, str]:
    """Compose a Telegram-safe plain-text leaderboard summary.

    Layout (high-detail blocks at top, scoreboard below):
      📊 Negev standings — TIMESTAMP
      ───────────────────  TRACKED 👥  ───────────────────
      👤 You (Igor)    full block: rank/total/split/vs-leader/vs-second
      👤 Friend1       same block + vs-you line
      ...one block per FRIEND_PARTICIPANTS entry...
      ─────────────────────  TOP 5  ──────────────────────
      1. Gilad   12.5
      ...
      ─────────────  AROUND YOU (rank ±2)  ──────────────
      ...

    Day-9.15: `rows` is the FULL roster (with bots) so ranks match the app.
    Top 5 / gap math filter bots out. Day-9.22 (this commit): symmetric
    tracked-people blocks at the top via core.reporting.people, so YOU and
    every friend in FRIEND_PARTICIPANTS get the same per-person audit
    (rank, total, group/futures split, vs leader/second/you).

    Returns: (title, body).
    """
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from core.reporting import people
    now = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Jerusalem"))
    title = f"📊 Negev standings — {now:%Y-%m-%d %H:%M IDT}"

    lines: list[str] = []
    by_name = {r["player"]: r for r in rows}
    me_row = by_name.get(me)
    n = len(rows)
    humans = [r for r in rows if r.get("role") != "bot"]
    leader_row = humans[0] if humans else rows[0]

    # ─── Tracked 👥 ─── one full audit block per (me + friends).
    tracked = people.tracked_participants()
    if tracked:
        lines.append("─" * 16 + "  TRACKED 👥  " + "─" * 16)
        for name in tracked:
            lines.append(people.render_block(rows, name, self_name=me))
            lines.append("")            # blank between blocks for breathing room
        # Trim the trailing blank
        if lines and lines[-1] == "":
            lines.pop()

    # ─── Top 5 humans ───
    lines.append("")
    lines.append("─" * 19 + "  TOP 5  " + "─" * 20)
    for r in humans[:5]:
        marker = "  ← you" if r["player"] == me else ""
        # Highlight any tracked friend in the top-5 list too
        if r["player"] != me and r["player"] in tracked:
            marker = "  ← tracked"
        lines.append(f"  {r['rank']:>2}. {r['player']:<18}  {r['total']:>6.1f}{marker}")

    # ─── Around you (if not in top 5/7) ───
    if me_row and me_row["rank"] > 7:
        lines.append("")
        lines.append("─" * 12 + "  AROUND YOU (rank ±2)  " + "─" * 12)
        my_rank = me_row["rank"]
        window = [r for r in rows if abs(r["rank"] - my_rank) <= 2]
        for r in window:
            marker = "  ← you" if r["player"] == me else ""
            if r["player"] != me and r["player"] in tracked:
                marker = "  ← tracked"
            lines.append(f"  {r['rank']:>2}. {r['player']:<18}  {r['total']:>6.1f}{marker}")

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
        # Day-9.15: fetch the FULL roster (bots + managers + players) so the
        # ranks in `rows` match what the Negev web app shows (Igor at 56/67,
        # not 26/63). The DB upsert below filters bots out so the strategy
        # layer's "gap to leader" math still compares only to real
        # competitors. Telegram message uses the full `rows` so the rank
        # number shown matches what the user sees in the app.
        with obs.external_call("negev_toto", "get_standings"):
            rows = ntm.toto_get_standings(tournament_id=tid,
                                           include_bots=True)
    except Exception as e:                             # noqa: BLE001
        log.error("Negev fetch failed: %s", e)
        return {"ok": False, "error": str(e)[:200]}

    if not rows:
        return {"ok": False, "error": "Negev returned 0 rows — auth failed or tournament empty"}

    # Day-9.15: filter bots from DB upsert so strategy stays clean. The
    # `include_bots` CLI flag, when True, keeps bots in the DB too — useful
    # for debug only; default flow strips them.
    rows_for_db = rows if include_bots else [r for r in rows
                                              if (r.get("role") != "bot")]

    # Day-9.15: NEW MEMBER DETECTION — compare Negev's roster to our DB,
    # find any displayName that's NEW (in Negev but not yet in our standings
    # table), and surface them in the result so the operator + Telegram
    # message can flag the addition. Auto-upserts new members alongside
    # existing ones — they appear in the next 📊 message automatically.
    new_members: list[str] = []
    if conn is not None and not dry:
        try:
            existing = {row[0] for row in conn.execute(
                "SELECT participant FROM standings").fetchall()}
            for r in rows_for_db:
                if r["player"] not in existing:
                    new_members.append(r["player"])
        except sqlite3.Error as e:
            log.warning("new-member detection failed: %s", e)

    n_upserted = 0
    for r in rows_for_db:
        try:
            _upsert_standings(conn, r, dry)
            n_upserted += 1
        except sqlite3.Error as e:
            log.warning("upsert %r failed: %s", r.get("player"), e)
    if not dry:
        conn.commit()
    if new_members:
        log.info("NEW MEMBERS detected and added to standings: %s", new_members)

    # Day-9.25: DEPARTED-MEMBER reconciliation. The UPSERT loop above keeps
    # the DB strictly inclusive of Negev — anyone still in Negev's roster
    # gets their row updated. But rows for people who LEFT Negev (or who
    # appear under a renamed `displayName`, creating a phantom duplicate)
    # never get cleaned up. Live evidence (2026-06-11): we had 66 rows in
    # standings but Negev had 65 humans, because two phantoms persisted
    # ("Yahav", "yahav sarfati") AND one new joiner ("Shuki") wasn't synced
    # yet. Phantoms pollute leaderboard rendering (`len(rows)` wrong) and
    # the strategy-tilt "gap-to-leader" math (a phantom could beat you).
    #
    # Reconciliation: after upsert, delete any standings row whose
    # participant is NOT in the current Negev roster. The DB now strictly
    # mirrors Negev. This runs only when we DID upsert (i.e. Negev fetch
    # succeeded with ≥1 row) so a transient empty fetch never wipes the
    # table.
    departed_members: list[str] = []
    if conn is not None and not dry and n_upserted > 0:
        try:
            current_negev = {r["player"] for r in rows_for_db}
            existing = {row[0] for row in conn.execute(
                "SELECT participant FROM standings").fetchall()}
            # Day-9.5: protect rows owned by MY_PARTICIPANT (the user might
            # be running both standings_set.py manual entry AND Negev sync;
            # we don't want to delete their own row even if their name
            # somehow doesn't match the Negev `displayName`).
            stale = existing - current_negev - ({me} if me else set())
            for participant in stale:
                conn.execute("DELETE FROM standings WHERE participant=?",
                              (participant,))
                departed_members.append(participant)
            if departed_members:
                conn.commit()
                log.info("DEPARTED MEMBERS removed from standings: %s",
                          departed_members)
        except sqlite3.Error as e:
            log.warning("departed-member reconciliation failed: %s", e)

    # `leader` / `gap to leader`: use the bot-filtered list so we don't
    # chase a phantom (bots randomly score every match — they'd top the
    # standings briefly and our strategy would tilt incorrectly).
    leader_rows = [r for r in rows if r.get("role") != "bot"]
    leader = leader_rows[0]
    my_row = next((r for r in rows if r["player"] == me), None)
    result = {
        "ok": True, "tournament_id": tid,
        "participants": len(rows), "upserted": n_upserted,
        "leader": leader["player"], "leader_total": leader["total"],
        "second_total": leader_rows[1]["total"] if len(leader_rows) > 1 else None,
    }
    if my_row:
        # `my_rank` matches the APP's rank (with bots in the list, sorted by
        # uid). `my_gap_to_leader` is vs the bot-filtered leader so the
        # number represents the gap to a real competitor.
        result.update({"my_rank": my_row["rank"], "my_total": my_row["total"],
                       "my_gap_to_leader": leader["total"] - my_row["total"]})
    else:
        result["warning"] = f"MY_PARTICIPANT={me!r} not in standings"
    if new_members:
        result["new_members"] = new_members      # Day-9.15
    if departed_members:
        result["departed_members"] = departed_members   # Day-9.25

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

    # Day-9.26: side-bet resolution watchdog. Fires its OWN Telegram alert
    # (not the standings summary) when a side-bet shell flips to
    # isResolved=true. Idempotent via the side_bet_state table — alerts
    # fire EXACTLY once per resolution. Safe on every cron tick.
    if not dry:
        try:
            from tools.sidebet_watch import detect_and_alert
            sb_out = detect_and_alert(conn, tid, ntm)
            result["side_bets_detected"] = sb_out.get("detected") or []
            result["side_bets_resolved_count"] = sb_out.get("resolved_count")
            if sb_out.get("errors"):
                result["side_bet_errors"] = sb_out["errors"]
        except Exception as e:                         # noqa: BLE001
            log.warning("side-bet watch failed: %s", e)
            result["side_bet_errors"] = [str(e)[:120]]

    # Telegram delivery — only on real sync (not dry-run) and only when asked.
    # Failure to deliver is non-fatal; the standings already wrote OK.
    if send_telegram and not dry:
        try:
            from core import delivery
            title, body = _format_telegram_summary(rows, me, tid)
            # Day-9.15: prepend a "new members joined" note when applicable
            if new_members:
                body = (f"👋 New member(s) joined: {', '.join(new_members)}\n\n"
                        + body)
            # Day-9.25: prepend a "departed/renamed" note when phantoms
            # were cleaned up. So the operator sees WHO got pruned in the
            # same message as the leaderboard — phantom drift is visible
            # at the moment it heals.
            if departed_members:
                body = (f"🧹 Removed stale roster entries: "
                        f"{', '.join(sorted(departed_members))}\n\n" + body)
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
