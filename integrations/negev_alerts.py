"""Telegram alerts for Negev MCP connection failures (Day-9.9).

Shared helper used by `tools/sync_negev_standings.py` + `tools/post_match_audit.py`.
Sends a ⚠ Telegram message when we can't reach the friends' Toto Firestore
backend, including the reason + a short remediation hint based on the error
classification (auth/network/config/empty).

Best-effort: if Telegram itself is down, log and continue — we never raise.

Day-9.32: respects MONDIAL_TESTING=1 (or =true). When set, both
`alert_failure` and `alert_failure_once_per_day` short-circuit BEFORE any
Telegram send or report file write — operator/admin scripts (one-shot
simulations, edge-case sweeps, dry-runs) can run as the production user
against the production DB without firing false-positive ⚠ alerts to the
shared channel. The suppression is logged once per call so the operator
sees it didn't silently drop a real failure.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from core.obs.logging import get_logger

log = get_logger("negev_alerts")


def _testing_mode() -> bool:
    """True when ad-hoc admin/test runs explicitly opt out of alerts.

    Truthy: '1', 'true', 'yes' (case-insensitive). Anything else (or unset) →
    production behavior. This is intentionally minimal: a single env var the
    operator sets at the shell when running one-shot scripts."""
    return os.environ.get("MONDIAL_TESTING", "").strip().lower() in (
        "1", "true", "yes")


def classify(reason: str) -> tuple[str, str]:
    """Return (category, hint) for an error message. Categories let the user
    spot the right fix at a glance without reading the raw exception."""
    r = (reason or "").lower()
    if "negev_tournament_id" in r or "negev_refresh_token" in r:
        return "config",  ("Check `.env` on the VM: NEGEV_TOURNAMENT_ID and "
                            "NEGEV_REFRESH_TOKEN must both be set.")
    if "0 rows" in r or "auth failed" in r:
        return "auth",    ("Likely refresh-token expired (~30 days). "
                            "Sign in to negev-toto.web.app → DevTools → "
                            "IndexedDB → firebaseLocalStorageDb → copy "
                            "stsTokenManager.refreshToken into "
                            "`.env::NEGEV_REFRESH_TOKEN`, then "
                            "`systemctl restart mondial2026`.")
    if "module not importable" in r or "no module named" in r:
        return "import",  ("integrations/negev_toto_mcp.py couldn't import — "
                            "check the venv: ls /home/mondial/mondial2026/"
                            ".venv/lib/python*/site-packages/")
    if "403" in r or "permission" in r or "denied" in r:
        return "rules",   ("Firestore security rules rejected the read. "
                            "Likely the doc path moved or your account lost "
                            "access. Run `tools/verify_negev_live.py` to "
                            "pinpoint which path 403s.")
    if "401" in r:
        return "auth",    ("ID-token rejected. Refresh token may have rotated "
                            "without notifying us — re-capture from DevTools.")
    if "timeout" in r or "timed out" in r or "connection" in r or "network" in r:
        return "network", ("Negev's Firestore is unreachable. Could be a "
                            "Hetzner outbound issue or Firebase outage. "
                            "Retry next cron slot.")
    return "unknown", ("Unclassified error. Read the raw reason below and "
                       "`journalctl -u mondial2026 -n 100` for context.")


# Day-9.23: in-process "first-failure-of-the-day" tracker so the daemon's
# 3 long-lived Negev call sites (daily_summary, kickoff_cards, build_card
# friend_picks) don't generate a Telegram storm if every match-window pass
# is hitting the same auth issue. Resets at midnight Asia/Jerusalem.
_LAST_ALERT_DATE: str | None = None


def alert_failure_once_per_day(*, source: str, reason: str,
                                  tz: str = "Asia/Jerusalem") -> bool:
    """Like alert_failure() but suppresses repeat alerts within the SAME
    local-day window. The first failure of the day fires Telegram; every
    subsequent failure same day is silent (still logged WARN by the caller).

    Used by daemon paths (daily_summary, kickoff_cards, build_card
    friend-picks) where a single auth break would otherwise produce
    dozens of identical Telegram alerts in 24h."""
    if _testing_mode():
        log.info("Negev alert SUPPRESSED (MONDIAL_TESTING=1): "
                 "source=%s reason=%r", source, (reason or "")[:120])
        return False
    global _LAST_ALERT_DATE
    today_local = datetime.now(timezone.utc).astimezone(
        ZoneInfo(tz)).strftime("%Y-%m-%d")
    if _LAST_ALERT_DATE == today_local:
        log.info("Negev alert suppressed (already alerted today %s)", today_local)
        return False
    sent = alert_failure(source=source, reason=reason)
    if sent:
        _LAST_ALERT_DATE = today_local
    return sent


def alert_failure(*, source: str, reason: str) -> bool:
    """Send a ⚠ Telegram alert for a Negev MCP connection failure.

    Args:
      source: short identifier of the calling script ("sync_negev_standings"
               or "post_match_audit"). Shows up as "Source:" in the message.
      reason: the raw error string (truncated to ~400 chars in the body).

    Returns True if Telegram delivery succeeded, False otherwise.
    Never raises — Telegram-down failures are logged and swallowed so the
    caller's exit code reflects the Negev failure, not the alert failure.
    """
    if _testing_mode():
        log.info("Negev alert SUPPRESSED (MONDIAL_TESTING=1): "
                 "source=%s reason=%r", source, (reason or "")[:120])
        return False
    try:
        from core import delivery
        category, hint = classify(reason)
        now_idt = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Jerusalem"))
        title = f"Negev MCP unreachable — {category}"
        body = (
            f"Source: {source}\n"
            f"Time:   {now_idt:%Y-%m-%d %H:%M IDT}\n"
            f"Category: {category}\n"
            f"\nReason:\n  {(reason or '?')[:400]}\n"
            f"\nAction:\n  {hint}\n"
            f"\nLogs: tail -50 /home/mondial/mondial2026/reports/cron-*.log"
        )
        ok = bool(delivery.alert(title, body))
        if ok:
            log.info("Negev failure alert sent (category=%s, source=%s)",
                     category, source)
        else:
            log.warning("Negev failure alert delivery returned False "
                        "(category=%s, source=%s)", category, source)
        return ok
    except Exception as e:                              # noqa: BLE001
        log.warning("Could not send Negev failure alert: %s "
                    "(original reason was: %s)", e, reason)
        return False
