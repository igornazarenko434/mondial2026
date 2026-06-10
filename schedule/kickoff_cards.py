"""Day-9.22: T+1m kickoff card — fires ~1 minute after each match's
kickoff and posts a Telegram message showing what YOU and every tracked
friend predicted.

WHY
===

The match cards (T-60m/-15m/-7m) tell us what the model recommends. The
kickoff card tells us what we actually LOCKED IN — a once-per-match "this
is what each of us is rooting for" snapshot the moment the whistle blows.
It's the most-read message of the day in a friends' pool: everyone wants
to see who picked the right side BEFORE the result is known.

WHEN
====

Fired by the same SchedulerDaemon loop as daily_summary. Once per match,
window-stamped ``kickoff`` in the runs ledger so:
  • Multiple ticks within the kickoff window don't double-send.
  • A daemon restart after kickoff still fires the card if it wasn't sent
    yet (catch-up — up to KICKOFF_CATCHUP_MIN minutes after KO).
  • Across days, idempotency keys never collide (match_id is unique).

The fire window is [kickoff + KICKOFF_DELAY_MIN, kickoff + KICKOFF_CATCHUP_MIN]
— defaults to 1-15 minutes after kickoff. Far enough out that everyone's
picks are locked; tight enough not to spam a match that finished hours ago.

WHAT IT CONTAINS
================

  ⚽ KICKOFF — Mexico vs South Africa (Group A)
  Stage:  Group · 19:00 IDT
  ─────────────────────────  PICKS 👥  ─────────────────────────
  Igor:    Mexico 2 — South Africa 1   ← you
  Vaadia:  Draw 1 — 1

  ───────────────────────  STARTING XI  ───────────────────────
  Mexico (4-3-3, Aguirre): Ochoa (GK), Galindo (DF), …
  South Africa (4-2-3-1, Broos): Williams (GK), …

  ───────────────────────  STANDINGS  ────────────────────────
  Igor:    0.0 pts   (rank 26/67)
  Vaadia:  3.5 pts   (rank 12/67, 3.5 ahead of you)

Lineup section is optional (api-football publishes ~1h before KO so it's
usually present; we degrade gracefully when not).

API COST
========

Per kickoff:
  • 1 Negev call (toto_get_match_details)
  • 1 Negev call (toto_get_standings) — only when tracked block enabled
  • 1 api-football call (lineups) — cached via Day-9.20 fixture cache
  • 1 api-football call (fixture id lookup, cached)

Per tournament total: ≤ 4 calls/kickoff × 104 matches = 416 calls. Both
budgets cover this with 4-5× headroom.
"""
from __future__ import annotations
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from core.obs.logging import get_logger
from core.obs.runs import RunLedger
from core import obs

log = get_logger("kickoff_cards")

# Tight fire window: send between T+1m and T+15m after kickoff. Earlier than
# +1m → some friends' picks may still be flushing into Firestore. Later than
# +15m → goals may have been scored; the card would feel stale. The window
# is configurable via env so an operator can widen catchup if the daemon
# crashes during a kickoff burst.
KICKOFF_DELAY_MIN = int(os.environ.get("KICKOFF_DELAY_MIN", "1"))
KICKOFF_CATCHUP_MIN = int(os.environ.get("KICKOFF_CATCHUP_MIN", "15"))


def _matches_due(conn: sqlite3.Connection, now_utc: datetime,
                 led: RunLedger) -> list[dict]:
    """Return matches whose kickoff falls in [now - CATCHUP, now - DELAY]
    AND haven't had a kickoff card delivered yet.

    Read-only against the matches table — never blocks the daemon loop."""
    try:
        upper = (now_utc - timedelta(minutes=KICKOFF_DELAY_MIN)).isoformat()
        lower = (now_utc - timedelta(minutes=KICKOFF_CATCHUP_MIN)).isoformat()
        rows = conn.execute(
            "SELECT match_id, utc_kickoff, stage, grp AS [group], home, away "
            "FROM matches WHERE utc_kickoff BETWEEN ? AND ? "
            "AND home IS NOT NULL AND away IS NOT NULL "
            "ORDER BY utc_kickoff",
            (lower, upper)).fetchall()
    except sqlite3.Error as e:
        log.warning("kickoff-due read failed: %s", e)
        return []
    due = []
    for r in rows:
        if led.was_handled(r["match_id"], "kickoff"):
            continue
        due.append({k: r[k] for k in r.keys()})
    return due


def _fetch_picks(home: str, away: str) -> tuple[list[dict] | None, dict | None]:
    """Return (friendsPicks, myPrediction) for this match. Wrapped in
    obs.external_call so cost + rate-limit are recorded."""
    try:
        from integrations import negev_toto_mcp as ntm
        with obs.external_call("negev_toto", "get_match_details"):
            details = ntm.toto_get_match_details(home=home, away=away)
        if "error" in (details or {}):
            log.info("kickoff picks: %s", details.get("error"))
            return None, None
        return details.get("friendsPicks"), details.get("myPrediction")
    except Exception as e:                                # noqa: BLE001
        log.warning("kickoff picks fetch failed for %s vs %s: %s", home, away, e)
        _maybe_alert_negev(f"kickoff_cards (_fetch_picks for {home} vs {away})", e)
        return None, None


def _fetch_standings_rows() -> list[dict]:
    """One Negev call — used to render compact standings lines per tracked
    person on the kickoff card. Empty list on any failure."""
    try:
        from integrations import negev_toto_mcp as ntm
        with obs.external_call("negev_toto", "get_standings"):
            return ntm.toto_get_standings(include_bots=True)
    except Exception as e:                                # noqa: BLE001
        log.warning("kickoff standings fetch failed: %s", e)
        _maybe_alert_negev("kickoff_cards (_fetch_standings_rows)", e)
        return []


def _maybe_alert_negev(source: str, e: Exception) -> None:
    """Day-9.23: ONCE-per-day Telegram alert helper for kickoff-card Negev
    failures. Best-effort — never propagates exceptions."""
    try:
        from integrations.negev_alerts import alert_failure_once_per_day
        alert_failure_once_per_day(source=source, reason=str(e))
    except Exception:                                     # noqa: BLE001
        pass


def _fetch_lineups(home: str, away: str, kickoff_iso: str
                    ) -> list[dict] | None:
    """Best-effort lineup pull from api-football. None on failure (api-
    football quota, name mismatch, or simply not yet published)."""
    try:
        from core.data import api_football as af
        fid = af.find_fixture_id(home, away, kickoff_iso)
        if not fid:
            return None
        return af.fetch_lineups(fid)
    except Exception as e:                                # noqa: BLE001
        log.warning("kickoff lineups fetch failed: %s", e)
        return None


def _format_lineup_compact(lineups: list[dict] | None,
                            home: str, away: str) -> list[str]:
    """One line per team: 'Mexico (4-3-3, Aguirre): Ochoa (GK), …'
    Capped at 8 starters per team to keep the message mobile-friendly."""
    if not lineups:
        return []
    lines = ["─" * 23 + "  STARTING XI  " + "─" * 23]
    by_team = {(L.get("team") or "").lower(): L for L in lineups}
    for team in (home, away):
        L = by_team.get(team.lower())
        if not L:
            lines.append(f"  {team}: (not posted)")
            continue
        formation = L.get("formation") or "?"
        coach = L.get("coach") or "?"
        xi = L.get("startXI") or []
        # 8 names keeps the line ~120 chars, ~2 visual lines on phones
        head = ", ".join(xi[:8])
        if len(xi) > 8:
            head += f", … +{len(xi)-8} more"
        lines.append(f"  {team} ({formation}, {coach}): {head}")
    return lines


def build_kickoff_text(match: dict, now_utc: datetime,
                       picks: list[dict] | None, my_pred: dict | None,
                       standings_rows: list[dict],
                       lineups: list[dict] | None,
                       *, tz: str = "Asia/Jerusalem") -> tuple[str, str]:
    """Compose (title, body) for ONE match. Pure function — easily unit-
    testable. Never raises."""
    from zoneinfo import ZoneInfo
    from core.reporting import people

    home, away = match.get("home", "?"), match.get("away", "?")
    stage = match.get("stage", "?")
    group = match.get("group") or match.get("grp") or ""
    ko_local = ""
    try:
        ko_local = datetime.fromisoformat(match["utc_kickoff"]) \
            .astimezone(ZoneInfo(tz)).strftime("%H:%M %Z")
    except Exception:                                     # noqa: BLE001
        pass

    title = f"⚽ KICKOFF — {home} vs {away}"
    lines = []
    stage_tag = f"{stage}" + (f" {group}" if group else "")
    lines.append(f"  Stage: {stage_tag}" + (f" · KO {ko_local}" if ko_local else ""))
    lines.append("")

    # ─── Picks (mine + tracked friends) ───
    tracked = people.tracked_participants()
    if tracked:
        lines.append("─" * 25 + "  PICKS 👥  " + "─" * 25)
        block = people.render_match_picks_block(picks, my_pred, tracked,
                                                  home, away)
        lines.append(block)

    # ─── Lineups (if available) ───
    lu_lines = _format_lineup_compact(lineups, home, away)
    if lu_lines:
        lines.append("")
        lines.extend(lu_lines)

    # ─── Compact standings per tracked person ───
    if tracked and standings_rows:
        lines.append("")
        lines.append("─" * 24 + "  STANDINGS  " + "─" * 23)
        me = people.my_participant()
        for name in tracked:
            lines.append(people.render_compact(standings_rows, name,
                                                 self_name=me))

    return title, "\n".join(lines)


def fire_due(conn: sqlite3.Connection, led: RunLedger, *,
             now: datetime | None = None) -> int:
    """For every match whose kickoff fell in [T+DELAY, T+CATCHUP] and hasn't
    been kickoff-carded yet: fetch picks + standings + lineups, render,
    deliver, mark in the ledger. Returns count sent.

    Concurrent kickoffs (e.g., the two simultaneous 22:00 IDT group-stage
    matches): each match gets its own message AND its own ledger row, but
    the standings snapshot is fetched ONCE per fire_due call and shared
    across all messages in this tick — saves N-1 Negev calls and means
    every message reflects the same canonical leaderboard moment. Per-match
    picks/lineups are still fetched individually (they're match-specific).

    Sends are serial: Telegram bot API caps at 1 msg/sec per chat, so two
    simultaneous kickoffs land ~1.x s apart (well under any practical
    limit). Per-match exceptions are caught so one failure can't block
    the sibling kickoff.

    Never raises."""
    now = now or datetime.now(timezone.utc)
    due = _matches_due(conn, now, led)
    if not due:
        return 0
    from core import delivery
    # ONE Negev standings call per fire_due → all messages in this tick
    # share the SAME snapshot (canonical leaderboard moment + saves N-1
    # API credits when N matches kick off together).
    shared_standings = _fetch_standings_rows() if due else []
    sent = 0
    for m in due:
        mid, home, away = m["match_id"], m["home"], m["away"]
        run_id = led.start(mid, "kickoff",
                            correlation_id=f"kickoff-{mid}")
        try:
            picks, my_pred = _fetch_picks(home, away)
            lineups = _fetch_lineups(home, away, m["utc_kickoff"])
            title, body = build_kickoff_text(m, now, picks, my_pred,
                                              shared_standings, lineups)
            ok = delivery.summary(title, body)
            led.finish(run_id, "ok" if ok else "failed",
                        detail=None if ok else "delivery returned False",
                        card_delivered=bool(ok))
            if ok:
                sent += 1
                log.info("kickoff card sent for match %s (%s vs %s)",
                          mid, home, away)
        except Exception as e:                            # noqa: BLE001
            led.finish(run_id, "failed", detail=str(e))
            log.error("kickoff card failed for match %s: %s", mid, e)
    return sent
