"""Watchdog — makes sure the system is alive and no job is silently stuck.

Two layers, by design:
  • PROCESS liveness  -> run the daemon under a supervisor (systemd/launchd) that
    restarts it if it dies. The daemon writes a HEARTBEAT file each tick; an
    external check (cron / the daily summary) alerts if the heartbeat goes stale
    (= the scheduler died).
  • JOB liveness       -> `check_stuck` finds runs that started but never finished
    (a hung/crashed pipeline) and alerts.
This is the best-practice split: the OS watches the process, the app watches jobs.
"""
from __future__ import annotations
import os
from datetime import datetime, timezone
from core.obs.runs import runs
from core.obs.logging import get_logger
from core import delivery

log = get_logger("watchdog")

HEARTBEAT_FILE = os.environ.get("HEARTBEAT_FILE",
                                os.path.join(os.path.dirname(__file__), "..", "store", "heartbeat"))
STUCK_MIN = int(os.environ.get("WATCHDOG_STUCK_MIN", "20"))
HEARTBEAT_MAX_AGE = int(os.environ.get("WATCHDOG_HEARTBEAT_MAX_AGE", "180"))  # seconds


def beat(path: str = HEARTBEAT_FILE) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(datetime.now(timezone.utc).isoformat())


def heartbeat_age(path: str = HEARTBEAT_FILE) -> float | None:
    if not os.path.exists(path):
        return None
    with open(path) as f:
        ts = datetime.fromisoformat(f.read().strip())
    return (datetime.now(timezone.utc) - ts).total_seconds()


def check_heartbeat(path: str = HEARTBEAT_FILE, max_age: int = HEARTBEAT_MAX_AGE,
                    notify: bool = True) -> bool:
    """True if healthy. Alerts (once) if the scheduler heartbeat is stale/missing."""
    age = heartbeat_age(path)
    healthy = age is not None and age <= max_age
    if not healthy and notify:
        delivery.alert("Scheduler DOWN",
                       f"heartbeat age={age}s (max {max_age}s) — scheduler may have died")
    return healthy


def check_stuck(older_than_min: int = STUCK_MIN, notify: bool = True) -> list[dict]:
    stuck = runs().stuck(older_than_min)
    if stuck and notify:
        delivery.alert("Stuck jobs",
                       "\n".join(f"match {s['match_id']} {s['window']} started {s['started_at']}"
                                 for s in stuck))
    return stuck


def run_checks() -> dict:
    """Call periodically (in the daemon loop and/or from a cron)."""
    return {"heartbeat_ok": check_heartbeat(), "stuck": check_stuck()}
