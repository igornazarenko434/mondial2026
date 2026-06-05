# Scheduling, watchdog & concurrency

## How the system knows when/who plays, and stays in sync (the data flow)
The calendar is **data, not hard-coded**, and the clock drives everything:

1. **Source of truth = football-data.org.** `football_data.ingest(conn)` pulls
   every WC match — `utcDate` (kickoff), `status`, `stage`, `group`, `homeTeam`,
   `awayTeam`, and `score` — and upserts them into the `matches` table (stage
   mapped to the rules stage; detonators tagged). Run once on Day 1, then
   **re-run periodically** (the daemon does this every `INGEST_EVERY_MIN`,
   default 30 min).
2. **Each tick the daemon reads `store.repo.upcoming_matches(conn)`** — scheduled
   matches kicking off within ~26h that have both teams (TBD knockout rows are
   skipped until resolved).
3. **`schedule.scheduler.due_jobs` computes the trigger times** from each match's
   `utc_kickoff`: T-24h/-60m/-15m/-7m = `kickoff − window`. A job is "due" when
   `now` is within ~3 min of that time (the daemon polls every 60s). So the
   triggers are derived per match from its own kickoff and today's clock — no
   global timetable to maintain.
4. **After a match finishes**, the next re-ingest sees `status=FINISHED` + the
   score (→ scoring/standings, who won) and, crucially, the **resolved knockout
   bracket**: football-data fills in the actual teams for the next round, so the
   previously-TBD fixtures now have `home`/`away` and `utc_kickoff` and
   automatically start appearing in `upcoming_matches` — the system picks up
   "who plays whom next" with zero manual input.
5. `store.repo.recent_finished(conn)` exposes just-finished matches for the
   results/scoring step and "who advanced" awareness.

So the loop is self-synchronizing: **ingest → store → poll upcoming → fire
windows → match finishes → re-ingest resolves results + next-round teams →
repeat**, every day, for whatever games are next.

## Do we have a scheduler? Yes.
`schedule/runner.py` is a long-running **daemon** that every `SCHED_POLL_SECONDS`
(default 60): refreshes the fixture list, finds jobs **due** in this window
(T-24h / T-60m / T-15m / T-7m via `schedule/scheduler.py`), dispatches each, writes
a heartbeat, and runs the watchdog. Jobs are de-duplicated, so a job is never run
twice. Run it: `python -m schedule.runner`.

## Do we need a watchdog? Yes — and it's two layers (best practice)
- **Process liveness → OS supervisor.** Run the daemon under **systemd** (Linux)
  or **launchd** (macOS) so it auto-restarts if it crashes. Example systemd unit:
  ```ini
  [Service]
  ExecStart=/path/.venv/bin/python -m schedule.runner
  Restart=always
  WorkingDirectory=/path/mondial2026
  EnvironmentFile=/path/mondial2026/.env
  ```
- **Job liveness → app watchdog** (`schedule/watchdog.py`). Each tick writes a
  **heartbeat** file; `check_heartbeat()` alerts if it goes stale (scheduler
  died), and `check_stuck()` alerts on runs that started but never finished
  (a hung pipeline). The **daily summary** is your external heartbeat: if it stops
  arriving, the scheduler is down.

Belt-and-suspenders: even if you forget the supervisor, a stale heartbeat or a
missing daily summary tells you the scheduler stopped.

## Two games at the same time — does it work? Yes.
The daemon uses a **ThreadPoolExecutor** (`SCHED_MAX_WORKERS`, default 4). When
two matches hit T-7m together, `tick()` submits **two independent jobs that run
concurrently** in separate threads. Verified by `tests/test_scheduler.py`
(`test_two_simultaneous_matches_run_concurrently`).

### Why threads, not multiprocessing
The work is **I/O-bound** — almost all time is spent waiting on API/odds/LLM HTTP
calls; the Dixon-Coles math is microseconds. Python releases the GIL during I/O,
so threads overlap perfectly and are far lighter than processes (no
pickling/IPC). **Multiprocessing would be the wrong tool** here (it's for
CPU-bound work). If you ever add heavy CPU work (e.g. a huge Monte-Carlo), run
just that in a `ProcessPoolExecutor` — but you don't need it for match-day.

### Safe under concurrency
- The **rate limiter** (token bucket) is thread-safe and shared per provider, so
  N concurrent jobs collectively stay within free-tier limits.
- The **cost & run ledgers** use `check_same_thread=False` + a lock (verified by
  `test_cost_ledger_thread_safe` writing from 4 threads).
- Each job is **stateless/idempotent** and failures are contained inside
  `process_match`, so one match can't break another.

## Config (env)
```
SCHED_POLL_SECONDS=60     SCHED_MAX_WORKERS=4
HEARTBEAT_FILE=store/heartbeat
WATCHDOG_STUCK_MIN=20     WATCHDOG_HEARTBEAT_MAX_AGE=180
```

## Optional upgrade
`APScheduler` can replace the poll-loop with exact-time triggers and missed-job
handling. It's a clean swap (the daemon's `tick`/job functions don't change), but
the dependency-free poll-loop is enough for this single-user system.
