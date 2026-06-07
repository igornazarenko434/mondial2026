"""Telegram alerts for Negev MCP connection failures (Day-9.9).

Shared helper used by `tools/sync_negev_standings.py` + `tools/post_match_audit.py`.
Sends a ⚠ Telegram message when we can't reach the friends' Toto Firestore
backend, including the reason + a short remediation hint based on the error
classification (auth/network/config/empty).

Best-effort: if Telegram itself is down, log and continue — we never raise.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from core.obs.logging import get_logger

log = get_logger("negev_alerts")


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
