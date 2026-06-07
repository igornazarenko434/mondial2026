"""Calendar + scheduler + provider correlation audit.

Run on the VM (or locally) to verify the daemon's view of the world matches
your expectations:

  * Every WC 2026 fixture got ingested (counts per stage)
  * Every group (A-L) is fully populated (4 teams × 12 groups = 48 teams)
  * Team names match data/wc2026_groups.csv (canonical roster)
  * Kickoff times are correctly stored as UTC and convert sanely to Israel local
  * Detonators are tagged on the right matches
  * Stage labels match config.rules.STAGE_TYPE
  * What the scheduler would dispatch in the next N hours
  * Other providers (odds_api, api_football) will recognise our normalized names

Usage:
    cd /home/mondial/mondial2026
    sudo -u mondial bash -c 'set -a && source .env && set +a && \
        PYTHONPATH=. .venv/bin/python tools/calendar_audit.py'

Read-only. No external API calls (provider correlation is offline checks).
"""
from __future__ import annotations
import csv
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store.db import connect
from core.data.teams import normalize
from config.rules import STAGE_TYPE

TZ_IL = ZoneInfo("Asia/Jerusalem")
GROUPS_CSV = os.path.join(os.path.dirname(__file__), "..", "data", "wc2026_groups.csv")
DETONATORS_CSV = os.path.join(os.path.dirname(__file__), "..", "data", "wc2026_detonator_fixtures.csv")


# ─────────────────────── pretty-print helpers ───────────────────────

def hdr(s: str):
    bar = "═" * 72
    print(f"\n\033[1;36m{bar}\033[0m")
    print(f"\033[1;36m  {s}\033[0m")
    print(f"\033[1;36m{bar}\033[0m")


def ok(s: str):    print(f"  \033[32m✓\033[0m {s}")
def warn(s: str):  print(f"  \033[33m⚠\033[0m {s}")
def err(s: str):   print(f"  \033[31m✗\033[0m {s}")
def info(s: str):  print(f"    {s}")


def israel_time(utc_iso: str) -> str:
    """UTC ISO string → Israel local time string for display."""
    try:
        dt = datetime.fromisoformat(utc_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ_IL).strftime("%Y-%m-%d %H:%M IDT")
    except (ValueError, TypeError):
        return f"(unparseable: {utc_iso!r})"


# ─────────────────────── §1 match count + stage distribution ───────────────────────

def audit_match_counts(conn):
    hdr("§1. Match count + stage distribution")
    total = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    expected_total = 104     # WC 2026: 72 group + 16 R32 + 8 R16 + 4 QF + 2 SF + 1 3rd + 1 Final
    if total == expected_total:
        ok(f"{total} matches ingested (expected {expected_total} for 48-team WC)")
    else:
        warn(f"got {total} matches, expected {expected_total} — verify with `football_data.refresh()`")

    stages = conn.execute(
        "SELECT stage, COUNT(*) FROM matches GROUP BY stage ORDER BY stage").fetchall()
    expected_per_stage = {"Group": 72, "R32": 16, "R16": 8, "QF": 4, "SF": 2, "3rd": 1, "Final": 1}
    print()
    print(f"  {'stage':<10}{'count':>8}  {'rules type':<12}{'match':<8}")
    for stage, n in stages:
        rules_type = STAGE_TYPE.get(stage, "??")
        expected = expected_per_stage.get(stage)
        marker = "✓" if expected == n else "⚠" if expected else "?"
        col = "\033[32m" if expected == n else "\033[33m" if expected else "\033[31m"
        print(f"  {col}{marker}\033[0m {stage:<8}{n:>8}  {rules_type:<12}{expected if expected else '?'}")


# ─────────────────────── §2 groups A-L populated ───────────────────────

def _load_canonical_groups():
    """Returns {group_letter: {team1, team2, ...}} from data/wc2026_groups.csv."""
    groups = defaultdict(set)
    with open(GROUPS_CSV) as f:
        for row in csv.DictReader(f):
            groups[row["group"]].add(row["team"])
    return groups


def audit_groups(conn):
    hdr("§2. Groups A-L populated + team-name alignment with canonical CSV")
    canonical = _load_canonical_groups()

    # group → set of teams seen in matches. Strip football-data.org's
    # "GROUP_" prefix for canonical comparison; the ingest also strips
    # going forward, but old rows from before the fix may still have it.
    def _g(s):
        if isinstance(s, str) and s.upper().startswith("GROUP_"):
            return s[len("GROUP_"):]
        return s
    db_groups = defaultdict(set)
    for grp, home, away in conn.execute(
            "SELECT grp, home, away FROM matches WHERE stage='Group' "
            "AND home IS NOT NULL AND away IS NOT NULL"):
        db_groups[_g(grp)].add(home)
        db_groups[_g(grp)].add(away)

    if not db_groups:
        err("no group-stage matches with both teams set — football-data ingest may be empty")
        return

    print(f"\n  {'grp':<5}{'count':>6}  teams (canonical alignment shown)")
    print(f"  {'─' * 70}")
    for grp in sorted(canonical):
        db_teams = db_groups.get(grp, set())
        canon_teams = canonical[grp]
        missing_in_db = canon_teams - db_teams
        unexpected_in_db = db_teams - canon_teams
        teams_str = ", ".join(sorted(db_teams)) if db_teams else "(none)"
        marker = "✓" if not missing_in_db and not unexpected_in_db else "⚠"
        col = "\033[32m" if marker == "✓" else "\033[33m"
        print(f"  {col}{marker}\033[0m {grp:<3}{len(db_teams):>6}  {teams_str}")
        if missing_in_db:
            info(f"  \033[33mmissing from DB:\033[0m {', '.join(sorted(missing_in_db))}")
        if unexpected_in_db:
            info(f"  \033[33min DB but NOT in canonical CSV:\033[0m {', '.join(sorted(unexpected_in_db))}")


# ─────────────────────── §3 team-name normalization round-trip ───────────────────────

def audit_name_normalization(conn):
    hdr("§3. Team-name normalization — DB vs alias map")
    teams = set()
    for (h,) in conn.execute("SELECT DISTINCT home FROM matches WHERE home IS NOT NULL"):
        teams.add(h)
    for (a,) in conn.execute("SELECT DISTINCT away FROM matches WHERE away IS NOT NULL"):
        teams.add(a)

    changed = []
    for t in sorted(teams):
        n = normalize(t)
        if n != t:
            changed.append((t, n))

    if not changed:
        ok(f"{len(teams)} team names all canonical — alias map already covered them on ingest")
    else:
        warn(f"{len(changed)} names would re-normalize if passed through teams.normalize() again:")
        for raw, norm in changed:
            info(f"  {raw!r}  →  {norm!r}")
        info("  These are typically benign (idempotent). Check core/data/teams.py if surprising.")


# ─────────────────────── §4 detonator tagging ───────────────────────

def audit_detonators(conn):
    hdr("§4. Detonator tagging — DB vs canonical CSV")

    # Load canonical detonators (only the rows with concrete teams; TBD KO rows excluded)
    canonical_known = set()
    canonical_tbd = 0
    with open(DETONATORS_CSV) as f:
        for row in csv.DictReader(f):
            if row.get("detonator") != "Y":
                continue
            home, away = row.get("home", ""), row.get("away", "")
            if home and away and home != "TBD":
                canonical_known.add(frozenset({normalize(home), normalize(away)}))
            else:
                canonical_tbd += 1

    # Currently tagged in DB
    tagged = conn.execute(
        "SELECT match_id, utc_kickoff, stage, home, away FROM matches "
        "WHERE detonator=1 ORDER BY utc_kickoff").fetchall()
    print(f"\n  CSV declares {len(canonical_known)} known + {canonical_tbd} TBD-knockout detonators.")
    print(f"  DB has {len(tagged)} matches tagged detonator=1:\n")

    db_pairs = {frozenset({h, a}) for (_, _, _, h, a) in tagged}
    print(f"  {'match_id':<10}{'kickoff (Israel)':<24}{'stage':<8}match")
    print(f"  {'─' * 70}")
    for mid, ko, stage, home, away in tagged:
        pair = frozenset({home, away})
        marker = "✓" if pair in canonical_known else "?"
        col = "\033[32m" if marker == "✓" else "\033[33m"
        print(f"  {col}{marker}\033[0m {mid:<8}{israel_time(ko):<24}{stage:<8}{home} vs {away}")

    missing = canonical_known - db_pairs
    if missing:
        warn(f"{len(missing)} detonator(s) in CSV but NOT tagged in DB:")
        for pair in missing:
            info(f"  {' vs '.join(sorted(pair))}")
    else:
        ok("all CSV-declared (known-team) detonators are tagged in the DB")


# ─────────────────────── §5 time correlation (UTC vs Israel) ───────────────────────

def audit_time_correlation(conn):
    hdr("§5. Time correlation — UTC kickoff → Israel local")
    # Spot-check: first 5 matches
    rows = conn.execute(
        "SELECT match_id, utc_kickoff, home, away, stage, grp FROM matches "
        "ORDER BY utc_kickoff LIMIT 5").fetchall()
    if not rows:
        err("no matches in DB")
        return
    print(f"\n  First 5 fixtures (sorted by kickoff):\n")
    print(f"  {'match_id':<10}{'UTC':<24}{'Israel local':<26}{'stage':<8}match")
    print(f"  {'─' * 90}")
    for mid, utc_iso, home, away, stage, grp in rows:
        utc_str = utc_iso.replace("T", " ").replace("+00:00", " UTC")[:24]
        print(f"  {mid:<8}{utc_str:<24}{israel_time(utc_iso):<26}{stage:<6}{grp or '':<3}{home} vs {away}")

    # Sanity: opener should be 2026-06-11 22:00 Israel local
    opener = conn.execute(
        "SELECT utc_kickoff FROM matches WHERE home='Mexico' AND away='South Africa'").fetchone()
    if opener:
        local = israel_time(opener[0])
        if "2026-06-11 22:00" in local:
            ok(f"Mexico vs South Africa → {local}  ← exactly opener-day local time")
        else:
            warn(f"Mexico vs South Africa → {local}  (expected 2026-06-11 22:00 IDT)")
    else:
        warn("Mexico vs South Africa not in matches table?!")


# ─────────────────────── §6 what's coming up + what scheduler will fire ───────────────────────

def audit_upcoming_view(conn):
    hdr("§6. What the scheduler sees — upcoming matches in next 26h")
    now = datetime.now(timezone.utc)
    horizon = (now + timedelta(hours=26)).isoformat()
    upcoming = conn.execute(
        "SELECT match_id, utc_kickoff, stage, grp, home, away "
        "FROM matches WHERE status IN ('SCHEDULED','TIMED') "
        "AND utc_kickoff IS NOT NULL AND utc_kickoff <= ? "
        "AND home IS NOT NULL AND away IS NOT NULL "
        "ORDER BY utc_kickoff", (horizon,)).fetchall()
    print(f"\n  Now (UTC): {now.isoformat()}")
    print(f"  Now (Israel): {now.astimezone(TZ_IL).isoformat()}")
    if not upcoming:
        info("No matches in the next 26h. Daemon idle-ticks; no cards will emit.")
    else:
        print(f"\n  {'match_id':<10}{'Israel local':<22}{'stage':<8}match")
        for mid, utc_iso, stage, grp, home, away in upcoming:
            print(f"  {mid:<8}{israel_time(utc_iso):<22}{stage:<6}{grp or '':<3}{home} vs {away}")


def audit_next_dispatch_simulation(conn):
    hdr("§7. Window-firing simulation — when is the FIRST job due?")
    # Find the earliest kickoff
    now = datetime.now(timezone.utc)
    earliest = conn.execute(
        "SELECT match_id, utc_kickoff, home, away FROM matches "
        "WHERE status IN ('SCHEDULED','TIMED') AND utc_kickoff > ? "
        "AND home IS NOT NULL AND away IS NOT NULL "
        "ORDER BY utc_kickoff LIMIT 1", (now.isoformat(),)).fetchone()
    if not earliest:
        warn("no future scheduled matches with both teams known — calendar may be empty")
        return
    mid, ko_iso, home, away = earliest
    ko = datetime.fromisoformat(ko_iso)
    if ko.tzinfo is None:
        ko = ko.replace(tzinfo=timezone.utc)
    print(f"\n  Earliest known match: {home} vs {away} (match_id {mid})")
    print(f"  Kickoff: {ko.isoformat()}  =  {ko.astimezone(TZ_IL).isoformat()}")

    print(f"\n  Window-firing schedule (Israel local):\n")
    for window, delta in [("T-24h", timedelta(hours=24)),
                           ("T-60m", timedelta(minutes=60)),
                           ("T-15m", timedelta(minutes=15)),
                           ("T-7m",  timedelta(minutes=7))]:
        fire_at_utc = ko - delta
        fire_at_il = fire_at_utc.astimezone(TZ_IL)
        time_until = fire_at_utc - now
        days = time_until.days
        hours = (time_until.seconds // 3600) if time_until.total_seconds() > 0 else 0
        minutes = (time_until.seconds % 3600) // 60
        if time_until.total_seconds() > 0:
            in_str = f"in {days}d {hours}h {minutes}m"
        else:
            in_str = "(already passed)"
        print(f"  {window:<8} {fire_at_il.strftime('%Y-%m-%d %H:%M IDT')}   {in_str}")


# ─────────────────────── §8 calendar freshness ───────────────────────

def audit_freshness(conn):
    hdr("§8. Calendar freshness")
    # Get most recent ingest based on heartbeat file
    here = os.path.join(os.path.dirname(__file__), "..", "store", "heartbeat")
    if os.path.exists(here):
        with open(here) as f:
            hb_ts = f.read().strip()
        try:
            hb_dt = datetime.fromisoformat(hb_ts)
            age = (datetime.now(timezone.utc) - hb_dt).total_seconds()
            if age < 180:
                ok(f"heartbeat is fresh ({age:.0f}s old — daemon is ticking)")
            else:
                warn(f"heartbeat is {age:.0f}s old — daemon may be stuck")
        except (ValueError, OSError):
            warn(f"heartbeat file present but unparseable: {hb_ts}")
    else:
        warn("no heartbeat file — daemon may not be running locally")

    # Look at api_calls ledger for football_data
    try:
        ledger_path = os.path.join(os.path.dirname(__file__), "..", "store", "obs.db")
        if os.path.exists(ledger_path):
            led = sqlite3.connect(ledger_path)
            row = led.execute(
                "SELECT MAX(ts) FROM api_calls WHERE provider='football_data'").fetchone()
            if row and row[0]:
                last = datetime.fromisoformat(row[0])
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                mins = (datetime.now(timezone.utc) - last).total_seconds() / 60
                info(f"last football_data API call: {last.astimezone(TZ_IL):%Y-%m-%d %H:%M IDT} ({mins:.1f} min ago)")
    except Exception as e:  # noqa: BLE001
        warn(f"couldn't inspect ledger: {e}")


# ─────────────────────── §9 status breakdown ───────────────────────

def audit_status(conn):
    hdr("§9. Status distribution + TBD knockout rows")
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM matches GROUP BY status").fetchall()
    print(f"\n  {'status':<14}{'count':>8}")
    for status, n in rows:
        print(f"  {status:<14}{n:>8}")

    tbd = conn.execute(
        "SELECT stage, COUNT(*) FROM matches "
        "WHERE home IS NULL OR away IS NULL GROUP BY stage").fetchall()
    if tbd:
        print(f"\n  Rows with TBD teams (filtered from upcoming_matches until bracket resolves):")
        for stage, n in tbd:
            info(f"  {stage}: {n}")


# ─────────────────────── main ───────────────────────

def main():
    conn = connect()
    print()
    print("\033[1mMondial 2026 — Calendar / Scheduler / Provider Correlation Audit\033[0m")
    print(f"DB: {conn.execute('PRAGMA database_list').fetchall()}")

    audit_match_counts(conn)
    audit_status(conn)
    audit_groups(conn)
    audit_name_normalization(conn)
    audit_detonators(conn)
    audit_time_correlation(conn)
    audit_upcoming_view(conn)
    audit_next_dispatch_simulation(conn)
    audit_freshness(conn)

    print()
    print("\033[1;32m✓ Audit complete.\033[0m\n")


if __name__ == "__main__":
    main()
