"""Day-9: 09:00-local daily summary (positive heartbeat + day-at-a-glance).

Each morning the daemon pushes a Telegram message:
  📅 today's games
  ✓ recent results
  Your score (from standings)
  Budget headline (Brave + odds_api free-tier consumption)

Why this exists, even though watchdog already alerts on failure:
  • Watchdog alerts only when something is broken — a quiet day looks
    identical to a dead daemon. The 09:00 message proves the system is
    alive every morning.
  • Same Telegram chat as cards/alerts (one place to monitor).
  • Idempotent — the (synthetic) match_id -1 + day-stamped window in the
    runs ledger means we never send twice per day, even across restarts.
"""
from __future__ import annotations
import os
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from core import delivery
from core.obs.runs import RunLedger
from core.obs.cost import ledger as cost_ledger
from core.obs.logging import get_logger

log = get_logger("daily_summary")

# Synthetic match_id reserved for daily-summary idempotency. Real match_ids
# from football-data.org are positive integers; -1 cannot collide.
DAILY_SUMMARY_MATCH_ID = -1


def _local_today_label(now_utc: datetime, tz: str = "Asia/Jerusalem") -> str:
    return now_utc.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d")


def already_sent_today(led: RunLedger, now_utc: datetime,
                       tz: str = "Asia/Jerusalem") -> bool:
    """True if today's summary has already been recorded in the runs ledger."""
    window = f"daily-summary-{_local_today_label(now_utc, tz)}"
    return led.was_handled(DAILY_SUMMARY_MATCH_ID, window)


def build_summary_text(conn: sqlite3.Connection, now_utc: datetime,
                       tz: str = "Asia/Jerusalem",
                       hour_local: int = 9) -> str:
    """Plain-text Telegram-safe summary. Never raises — failures degrade to
    sections being omitted rather than the whole summary failing.

    `hour_local` is the local hour at which the daily summary fires (default 9).
    Today's-games window extends from local midnight TODAY through
    `hour_local:00 TOMORROW` so that night kickoffs (e.g. 02:00 / 06:00 local
    of the next calendar day) appear in TONIGHT's summary instead of being
    silently dropped into a future-day window that fires after kickoff.
    """
    tz_obj = ZoneInfo(tz)
    today_local = now_utc.astimezone(tz_obj).date()

    # Today's games — query the matches table directly using the injected
    # `now_utc` so the summary is reproducible and testable (repo.upcoming_
    # matches reads the real wall clock and would skip future-dated test
    # fixtures during unit tests).
    today_games: list[str] = []
    has_overnight = False
    try:
        from datetime import timedelta
        # local-day window: midnight TODAY through hour_local:00 TOMORROW.
        # Boundary aligns with the next daily summary's start, so each
        # match is listed in EXACTLY ONE summary — no gaps, no dupes.
        local_midnight = datetime.combine(today_local,
                                          datetime.min.time(), tzinfo=tz_obj)
        local_window_end = (local_midnight + timedelta(days=1)).replace(
            hour=hour_local, minute=0, second=0)
        local_today_end = local_midnight.replace(hour=23, minute=59, second=59)
        day_start_utc = local_midnight.astimezone(timezone.utc).isoformat()
        day_end_utc = local_window_end.astimezone(timezone.utc).isoformat()
        today_end_utc = local_today_end.astimezone(timezone.utc).isoformat()
        rows = conn.execute(
            "SELECT match_id, utc_kickoff, stage, home, away FROM matches "
            "WHERE status IN ('SCHEDULED','TIMED') AND utc_kickoff IS NOT NULL "
            "AND utc_kickoff BETWEEN ? AND ? "
            "AND home IS NOT NULL AND away IS NOT NULL "
            "ORDER BY utc_kickoff",
            (day_start_utc, day_end_utc)).fetchall()
        for m in rows:
            ko = datetime.fromisoformat(m["utc_kickoff"]).astimezone(tz_obj)
            today_games.append(
                f"{ko.strftime('%H:%M')} {m['home']} vs {m['away']} "
                f"({m['stage']})")
            if m["utc_kickoff"] > today_end_utc:
                has_overnight = True
    except Exception as e:                          # noqa: BLE001
        log.warning("today's-games read failed: %s", e)

    # Recently finished — last 30h relative to the injected `now_utc`.
    results: list[str] = []
    try:
        from datetime import timedelta
        since = (now_utc - timedelta(hours=30)).isoformat()
        rows = conn.execute(
            "SELECT match_id, home, away, home_goals, away_goals "
            "FROM matches WHERE status='FINISHED' AND utc_kickoff >= ? "
            "ORDER BY utc_kickoff DESC", (since,)).fetchall()
        for m in rows:
            results.append(
                f"{m['home']} {m['home_goals']}-{m['away_goals']} {m['away']}")
    except Exception as e:                          # noqa: BLE001
        log.warning("recent-finished read failed: %s", e)

    # Standings — read MY row by the configured participant label. Same
    # MY_PARTICIPANT env var the scheduler uses for update_standings() and
    # the strategy layer's standings_context, so the three writers/readers
    # always reference the SAME row (e.g. "Igor" on prod, "me" in tests).
    me = os.environ.get("MY_PARTICIPANT", "me").strip() or "me"
    stand = None
    try:
        # Day-9.27: include side_points so the fallback line shows the
        # SAME total the Negev app shows. COALESCE guards legacy NULL.
        stand = conn.execute(
            "SELECT group_points, knockout_points, futures_points, "
            "COALESCE(side_points, 0) AS side_points, "
            "(group_points + knockout_points + futures_points "
            " + COALESCE(side_points, 0)) AS total "
            "FROM standings WHERE participant=?", (me,)
        ).fetchone()
    except Exception as e:                          # noqa: BLE001
        log.warning("standings read failed: %s", e)

    # Day-9.22: Tracked-people blocks — pull fresh Negev rows so rank/total
    # match the app exactly + every friend gets the same per-person audit.
    # ONE Negev API call per delivered summary (= 1/day). Falls back silently
    # if Negev unreachable; the local-DB "Your score" line below still fires.
    from core.reporting import people
    tracked = people.tracked_participants()
    negev_rows: list[dict] = []
    if tracked:
        try:
            from integrations import negev_toto_mcp as ntm
            from core import obs
            with obs.external_call("negev_toto", "get_standings"):
                negev_rows = ntm.toto_get_standings(include_bots=True)
        except Exception as e:                       # noqa: BLE001
            log.warning("Negev fetch for tracked blocks failed: %s", e)
            negev_rows = []
            # Day-9.23: fire ⚠ Telegram ONCE per day so we know the daemon's
            # Negev path is broken before 24h of silent degradation accumulates.
            try:
                from integrations.negev_alerts import alert_failure_once_per_day
                alert_failure_once_per_day(
                    source="daily_summary (build_summary_text)", reason=str(e))
            except Exception:                          # noqa: BLE001
                pass    # alerts are best-effort, must never crash the daemon

    # Budget headline — only providers with a real budget contribute a number
    L = cost_ledger()
    brave = L.quota_status("brave_search")
    odds = L.quota_status("odds_api")

    lines = [f"📅 Mondial 2026 — {today_local.isoformat()}"]
    if today_games:
        n_today = len(today_games)
        # Note overnight extension when a listed kickoff falls in the
        # 00:00–hour_local local window of the NEXT calendar day.
        suffix = (f" — through tomorrow {hour_local:02d}:00"
                  if has_overnight else "")
        lines.append(
            f"Today ({n_today} game{'s' if n_today != 1 else ''}){suffix}:")
        for g in today_games[:5]:
            lines.append(f"  • {g}")
    else:
        lines.append("No games today.")
    if results:
        lines.append(f"Recent ({len(results)}):")
        for r in results[:4]:
            lines.append(f"  ✓ {r}")
    # Day-9.22: per-tracked-person compact line (replaces the legacy single
    # "Your score" row). Falls back to the legacy line only when Negev
    # didn't load (rare; keeps the summary useful in degraded mode).
    if negev_rows and tracked:
        lines.append("")
        lines.append("Tracked 👥:")
        for name in tracked:
            lines.append(people.render_compact(negev_rows, name, self_name=me))
    elif stand:
        # Day-9.27: include side in the breakdown so the fallback line
        # mirrors the Negev app's 4-column split.
        lines.append(
            f"Your score: {stand['total']:.1f} pts "
            f"(group {stand['group_points']:.1f} / "
            f"KO {stand['knockout_points']:.1f} / "
            f"side {stand['side_points']:.1f} / "
            f"futures {stand['futures_points']:.1f})")
    lines.append(
        f"Budget: Brave {brave.get('used', 0):.0f}/{brave.get('budget') or '∞'}  "
        f"odds {odds.get('used', 0):.0f}/{odds.get('budget') or '∞'}")
    return "\n".join(lines)


def send_if_due(conn: sqlite3.Connection, led: RunLedger, *,
                now: datetime | None = None,
                hour_local: int = 9, tz: str = "Asia/Jerusalem") -> bool:
    """If `now` is past `hour_local` in `tz` and today's summary hasn't been
    sent, build + send via delivery.alert. Returns True if SENT this call.

    The runs-ledger row is created BEFORE the send so a delivery failure
    doesn't retry-storm us — better to miss one summary than to flood the
    chat. The next morning's call picks up cleanly (different day label).
    """
    now = now or datetime.now(timezone.utc)
    local_now = now.astimezone(ZoneInfo(tz))
    if local_now.hour < hour_local:
        return False
    if already_sent_today(led, now, tz):
        return False
    day_label = _local_today_label(now, tz)
    window_label = f"daily-summary-{day_label}"
    run_id = led.start(DAILY_SUMMARY_MATCH_ID, window_label,
                        correlation_id=window_label)
    try:
        body = build_summary_text(conn, now, tz, hour_local=hour_local)
        # `summary` (not `alert`) — keeps the ☀️ emoji clean without ⚠️ prefix
        ok = delivery.summary(f"☀️ Daily summary — {day_label}", body)
        led.finish(run_id, "ok" if ok else "failed",
                    detail=None if ok else "delivery returned False",
                    card_delivered=bool(ok))
        log.info("daily summary sent for %s (ok=%s)", day_label, ok)
        return bool(ok)
    except Exception as e:                          # noqa: BLE001
        led.finish(run_id, "failed", detail=str(e))
        log.error("daily summary failed: %s", e)
        return False
