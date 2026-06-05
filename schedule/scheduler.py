"""Per-match job scheduling: compute the T-24h/-60m/-15m/-7m trigger times from
each match's kickoff, and decide which are DUE now.

Production semantics (best practice for time-triggered jobs):
- **Catch-up, not exact-instant.** A window is due once its time has arrived and
  the match hasn't started — and we cap how *late* we'll still fire it
  (`catchup_min`). This means a daemon restart shortly before kickoff still fires
  the all-important pre-kickoff window, while ancient windows (e.g. T-24h seen
  for the first time 20h late) are skipped.
- **Idempotent.** An optional `is_done(match_id, window)` predicate (backed by the
  persistent runs ledger) prevents re-firing a window already handled — so a
  restart never re-sends a card.
- **UTC throughout**; naive kickoff strings are coerced to UTC defensively.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Callable

WINDOWS = {"T-24h": timedelta(hours=24), "T-60m": timedelta(minutes=60),
           "T-15m": timedelta(minutes=15), "T-7m": timedelta(minutes=7)}


def _parse_utc(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def jobs_for_match(match: dict) -> list[dict]:
    """The four scheduled jobs for one match, with run_at = kickoff - window."""
    ko = _parse_utc(match["utc_kickoff"])
    return [{"match_id": match["match_id"], "window": w,
             "run_at": (ko - delta).isoformat()}
            for w, delta in WINDOWS.items()]


def due_jobs(matches: list[dict], now: datetime | None = None,
             catchup_min: int = 120,
             is_done: Callable[[object, str], bool] | None = None) -> list[dict]:
    """Jobs due now: window time reached, <= catchup_min late, match not started,
    and not already handled."""
    now = now or datetime.now(timezone.utc)
    out = []
    for m in matches:
        ko = _parse_utc(m["utc_kickoff"])
        if ko <= now:                       # match started / finished -> nothing to do
            continue
        for w, delta in WINDOWS.items():
            run_at = ko - delta
            late = (now - run_at).total_seconds()
            if 0 <= late <= catchup_min * 60:        # arrived, not too stale
                if is_done and is_done(m["match_id"], w):
                    continue
                out.append({"match_id": m["match_id"], "window": w,
                            "run_at": run_at.isoformat()})
    return out
