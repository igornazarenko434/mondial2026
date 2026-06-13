"""Day-9.26: side-bet resolution detector + auto Telegram alert.

Negev's app shows per-user side-bet points for everyone, but the per-user
pick docs live at a Firestore path our regular-user auth cannot read (403
across every probed convention). However, we CAN read:
  - The side-bet shell at tournaments/{tid}/sideBets/{sb_id}
  - Including isResolved + correctAnswer

So we detect when a shell flips to resolved (via SQLite state table for
idempotency), and immediately fire a Telegram alert with:
  - The question + correct answer
  - Ready-to-paste CLI commands per tracked friend so the operator can
    update overrides in a couple of seconds:
      tools/standings_set.py side-bet "Igor" <pts>

That collapses the manual step from "remember to do this after each side
bet" to "tap the line from Telegram." Idempotency: each shell triggers
exactly ONE alert per resolution.

Called from sync_negev_standings.py after the bet aggregation runs (so
the alert lands in the same cron tick as the new standings update).
"""
from __future__ import annotations
import logging
import os
import sqlite3
from datetime import datetime, timezone

log = logging.getLogger("sidebet_watch")


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent CREATE for older DBs. New deployments get it via schema.sql."""
    conn.execute("""CREATE TABLE IF NOT EXISTS side_bet_state (
        side_bet_id     TEXT PRIMARY KEY,
        tournament_id   TEXT NOT NULL,
        question        TEXT,
        correct_answer  TEXT,
        is_resolved     INTEGER DEFAULT 0,
        notified_at     TEXT,
        seen_at         TEXT)""")
    conn.commit()


def _cumulative_so_far(conn: sqlite3.Connection, tid: str) -> int:
    """How many side bets have RESOLVED in this tournament so far (according
    to our local state table). Used to seed the operator's CLI command —
    each resolved side bet adds 1 pt to anyone who got it right, so the
    cumulative side-bet pts a tracked friend SHOULD have if they got every
    one right = resolved-count."""
    row = conn.execute(
        "SELECT COUNT(*) FROM side_bet_state "
        "WHERE tournament_id=? AND is_resolved=1", (tid,)).fetchone()
    return int(row[0] or 0) if row else 0


def detect_and_alert(conn: sqlite3.Connection, tid: str,
                       ntm,
                       tracked_names: list[str] | None = None,
                       send_telegram=None,
                       now: datetime | None = None) -> dict:
    """Scan Negev for newly-resolved side bets; alert + persist state.

    Args:
      conn:           local SQLite (mondial.db) for the side_bet_state table
      tid:            tournament id (Negev)
      ntm:            negev_toto_mcp module (injectable for tests)
      tracked_names:  list of displayName strings to pre-build CLI commands
                      for. None → just operator's name from MY_PARTICIPANT.
      send_telegram:  callable(title, body) → bool. None → core.delivery.summary
      now:            override for testing; default UTC now

    Returns: {detected: [...sb_ids...], alerted: [...], errors: [...]}
    """
    _migrate(conn)
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")

    # 1. Pull current shells from Negev
    try:
        shells = ntm._read_all(f"tournaments/{tid}/sideBets")
    except Exception as e:                                  # noqa: BLE001
        log.warning("sidebet_watch: failed to read shells: %s", e)
        return {"detected": [], "alerted": [], "errors": [str(e)[:120]]}

    # 2. Load known state
    known = {}
    for row in conn.execute(
            "SELECT side_bet_id, is_resolved, notified_at FROM side_bet_state "
            "WHERE tournament_id=?", (tid,)).fetchall():
        known[row[0]] = {"is_resolved": bool(row[1]), "notified_at": row[2]}

    if tracked_names is None:
        tracked_names = []
        me = os.environ.get("MY_PARTICIPANT", "").strip()
        if me:
            tracked_names.append(me)
        friends_csv = os.environ.get("FRIEND_PARTICIPANTS", "")
        tracked_names += [s.strip() for s in friends_csv.split(",")
                            if s.strip()]
    tracked_names = list(dict.fromkeys(tracked_names))   # dedup, preserve order

    detected, alerted, errors = [], [], []
    for shell in shells:
        sb_path = shell.get("_path", "")
        sb_id = sb_path.split("/")[-1] if sb_path else None
        if not sb_id:
            continue
        is_resolved = bool(shell.get("isResolved"))
        question = shell.get("question") or ""
        correct = shell.get("correctAnswer")

        prev = known.get(sb_id)

        # NEW resolution: not seen before OR previously unresolved → resolved
        is_new_resolution = (
            is_resolved and (prev is None or not prev["is_resolved"]))

        # Always persist the latest state, even if not new
        try:
            conn.execute(
                "INSERT INTO side_bet_state "
                "(side_bet_id, tournament_id, question, correct_answer, "
                " is_resolved, seen_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(side_bet_id) DO UPDATE SET "
                " question = excluded.question, "
                " correct_answer = excluded.correct_answer, "
                " is_resolved = excluded.is_resolved, "
                " seen_at = excluded.seen_at",
                (sb_id, tid, question, correct, int(is_resolved), now_iso))
            conn.commit()
        except sqlite3.Error as e:
            log.warning("sidebet_watch: state upsert failed for %s: %s",
                          sb_id, e)
            errors.append(f"upsert {sb_id}: {e}")
            continue

        if not is_new_resolution:
            continue
        detected.append(sb_id)

        # Day-9.27: now that toto_get_standings reads tournamentStats
        # directly (which includes side bet points pre-computed by Negev),
        # the operator no longer needs to manually update overrides — the
        # standings sync auto-picks up the new side-bet pts.
        # Telegram alert is now PURELY INFORMATIONAL: shows the question,
        # correct answer, and who from the tracked friends got it right.
        title = "🎯 Side bet resolved"
        body_lines = [
            f"🎯 Side bet resolved",
            "",
            f"❓ {question}",
            f"✅ Correct answer: {correct or '(unknown)'}",
            "",
        ]
        # Pull voters via the Day-9.27 tool — purely informational; the
        # standings sync already auto-credits via tournamentStats.
        try:
            voters = ntm.toto_get_side_bet_voters(sb_id, tournament_id=tid)
            winner_names = {w.get("player") for w in
                              (voters.get("winners") or [])}
            tracked_set = set(tracked_names)
            won = sorted(tracked_set & winner_names)
            lost = sorted(tracked_set - winner_names)
            if won:
                body_lines.append(f"🏆 Tracked friends who got +1 pt:")
                for n in won:
                    body_lines.append(f"   • {n}")
            if lost:
                body_lines.append(f"😬 Tracked friends who missed:")
                for n in lost:
                    body_lines.append(f"   • {n}")
            body_lines += [
                "",
                f"Community: {voters.get('yes_count', '?')} Yes · "
                f"{voters.get('no_count', '?')} No",
                "",
                "Standings will auto-update on the next sync (within 2h)."
            ]
        except Exception as ex:                         # noqa: BLE001
            log.warning("voters lookup failed for %s: %s", sb_id, ex)
            body_lines += [
                f"Open negev-toto.web.app → Side Bets → click 'Click-to-Expand'",
                f"on the resolved bet to see who got it right.",
                "",
                "Standings will auto-update on the next sync (within 2h)."
            ]
        body = "\n".join(body_lines)

        if send_telegram is None:
            try:
                from core import delivery
                send_telegram = delivery.summary
            except Exception:                              # noqa: BLE001
                send_telegram = None

        try:
            ok = send_telegram(title, body) if send_telegram else False
            if ok:
                conn.execute(
                    "UPDATE side_bet_state SET notified_at=? "
                    "WHERE side_bet_id=?", (now_iso, sb_id))
                conn.commit()
                alerted.append(sb_id)
                log.info("sidebet_watch: alerted on %s", sb_id)
            else:
                log.warning("sidebet_watch: alert failed for %s", sb_id)
                errors.append(f"alert {sb_id}: delivery returned false")
        except Exception as e:                              # noqa: BLE001
            log.warning("sidebet_watch: alert exception for %s: %s",
                          sb_id, e)
            errors.append(f"alert {sb_id}: {e}")

    return {"detected": detected, "alerted": alerted, "errors": errors,
            "shells_seen": len(shells), "resolved_count": _cumulative_so_far(conn, tid)}


def main(argv: list[str] | None = None) -> int:
    """CLI for manual runs:
       tools/sidebet_watch.py             # scan + alert
       tools/sidebet_watch.py --dry       # scan + log, no Telegram
       tools/sidebet_watch.py --reset     # wipe local state (re-arms alerts)
    """
    import argparse
    p = argparse.ArgumentParser(prog="sidebet_watch")
    p.add_argument("--dry", action="store_true")
    p.add_argument("--reset", action="store_true",
                   help="WIPE side_bet_state for this tournament — "
                          "next run re-alerts on every resolved side bet")
    p.add_argument("--tid", default=None)
    args = p.parse_args(argv)

    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from store.db import connect
    from integrations import negev_toto_mcp as ntm

    tid = args.tid or os.environ.get("NEGEV_TOURNAMENT_ID", "").strip()
    if not tid:
        print("✗ NEGEV_TOURNAMENT_ID not set", file=sys.stderr)
        return 2

    conn = connect()
    try:
        _migrate(conn)
        if args.reset:
            conn.execute("DELETE FROM side_bet_state WHERE tournament_id=?",
                          (tid,))
            conn.commit()
            print(f"✓ wiped side_bet_state for {tid}")
            return 0
        send = None
        if args.dry:
            send = lambda title, body: print(
                f"[dry-run]\n  title: {title}\n  body:\n{body}\n") or True
        out = detect_and_alert(conn, tid, ntm, send_telegram=send)
        print(f"  shells:    {out.get('shells_seen')}")
        print(f"  resolved:  {out.get('resolved_count')}")
        print(f"  detected:  {out.get('detected') or '(none new)'}")
        print(f"  alerted:   {out.get('alerted') or '(none)'}")
        print(f"  errors:    {out.get('errors') or '(none)'}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    sys.exit(main())
