"""Day-9.5: enter / view / import friends' standings.

The Negev Toto app uses Firebase auth we deliberately don't depend on, so
friends' point totals are entered manually here. Cadence: after each
match day, copy the leaderboard from the app into this tool. 2 minutes.

Usage:
  # See current standings (sorted by total desc)
  python tools/standings_set.py list

  # Set / update one participant
  python tools/standings_set.py set "Igor" --group 24.5 --ko 0 --futures 4.2

  # Bulk import from a JSON file (preferred — fewer typos):
  #   [{"participant":"Igor","group_points":24.5,"knockout_points":0,
  #     "futures_points":4.2}, ...]
  python tools/standings_set.py import friends.json

  # Remove a participant (e.g. someone dropped out)
  python tools/standings_set.py remove "John"

Conventions matching the rest of the system:
  * `group_points`: enter the value AFTER the §14 -15 % reset if KO has
    started — i.e. exactly what the Negev app shows for that participant.
    The reader (store.repo.standings_context) sums columns raw, trusting
    that you've already entered the reset value.
  * `participant` name must match MY_PARTICIPANT in .env for YOUR row to
    be recognised by the strategy layer.
  * Writes to the same SQLite file the daemon reads from
    (/home/mondial/mondial2026/store/mondial.db on the VM).
"""
from __future__ import annotations
import argparse
import json
import os
import sqlite3
import sys

# Make this script runnable from anywhere on the VM
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store.db import connect


def _all_rows(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        "SELECT participant, group_points, knockout_points, futures_points, "
        "(group_points + knockout_points + futures_points) AS total "
        "FROM standings ORDER BY total DESC, participant ASC")
    return [dict(row) for row in cur.fetchall()]


def cmd_list(args, conn) -> int:
    rows = _all_rows(conn)
    if not rows:
        print("(no standings entered yet)")
        print(f"  → add one with:  python {sys.argv[0]} set NAME --group X --ko Y --futures Z")
        return 0
    me = os.environ.get("MY_PARTICIPANT", "")
    print(f"{'rank':>4}  {'participant':<20}  {'group':>7}  {'ko':>6}  {'futures':>7}  {'total':>7}")
    print("-" * 64)
    for i, r in enumerate(rows, 1):
        marker = "  ← you" if r["participant"] == me else ""
        print(f"{i:>4}  {r['participant']:<20}  "
              f"{r['group_points']:>7.2f}  {r['knockout_points']:>6.2f}  "
              f"{r['futures_points']:>7.2f}  {r['total']:>7.2f}{marker}")
    if me and not any(r["participant"] == me for r in rows):
        print(f"\n  ⚠ MY_PARTICIPANT={me!r} not in standings — strategy layer "
              "will no-op until you add your row.")
    return 0


def _upsert(conn: sqlite3.Connection, participant: str,
            group_points: float, knockout_points: float,
            futures_points: float) -> None:
    conn.execute(
        "INSERT INTO standings (participant, group_points, knockout_points, "
        "futures_points) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(participant) DO UPDATE SET "
        "group_points = excluded.group_points, "
        "knockout_points = excluded.knockout_points, "
        "futures_points = excluded.futures_points",
        (participant, float(group_points), float(knockout_points),
         float(futures_points)))
    conn.commit()


def cmd_set(args, conn) -> int:
    if any(v is None for v in (args.group, args.ko, args.futures)):
        print("error: --group, --ko, --futures all required", file=sys.stderr)
        return 2
    _upsert(conn, args.participant, args.group, args.ko, args.futures)
    print(f"✓ set {args.participant}: group={args.group}, ko={args.ko}, "
          f"futures={args.futures}, total={args.group + args.ko + args.futures:.2f}")
    return cmd_list(args, conn)


def cmd_remove(args, conn) -> int:
    cur = conn.execute("DELETE FROM standings WHERE participant=?",
                       (args.participant,))
    conn.commit()
    if cur.rowcount == 0:
        print(f"✗ {args.participant!r} not found in standings")
        return 1
    print(f"✓ removed {args.participant}")
    return cmd_list(args, conn)


def cmd_import(args, conn) -> int:
    if not os.path.exists(args.path):
        print(f"error: file not found: {args.path}", file=sys.stderr)
        return 2
    try:
        with open(args.path) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON in {args.path}: {e}", file=sys.stderr)
        return 2
    if not isinstance(data, list):
        print("error: JSON must be a list of {participant, group_points, "
              "knockout_points, futures_points} objects", file=sys.stderr)
        return 2
    n = 0
    for row in data:
        try:
            _upsert(conn, row["participant"],
                    row.get("group_points", 0.0),
                    row.get("knockout_points", 0.0),
                    row.get("futures_points", 0.0))
            n += 1
        except KeyError as e:
            print(f"  ⚠ skipping row missing {e}: {row}")
        except (TypeError, ValueError) as e:
            print(f"  ⚠ skipping invalid row {row}: {e}")
    print(f"✓ imported {n}/{len(data)} rows from {args.path}")
    return cmd_list(args, conn)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="standings_set",
                                description="Enter friends' Toto standings.")
    p.add_argument("--db", default=None,
                   help="Path to mondial.db (default: store/mondial.db relative to repo)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show current standings")

    s_set = sub.add_parser("set", help="set / update one participant")
    s_set.add_argument("participant")
    s_set.add_argument("--group",   type=float, dest="group")
    s_set.add_argument("--ko",      type=float, dest="ko")
    s_set.add_argument("--futures", type=float, dest="futures")

    s_rm = sub.add_parser("remove", help="delete a participant row")
    s_rm.add_argument("participant")

    s_imp = sub.add_parser("import", help="bulk-import from a JSON file")
    s_imp.add_argument("path")

    args = p.parse_args(argv)

    conn = connect(args.db) if args.db else connect()
    try:
        return {
            "list":   cmd_list,
            "set":    cmd_set,
            "remove": cmd_remove,
            "import": cmd_import,
        }[args.cmd](args, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
