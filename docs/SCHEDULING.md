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

## Day-9: always-on hosting on Hetzner

Why hosted: your Mac sleeps / closes lid / loses Wi-Fi — none of those are
allowed during a T-7m window. A €5 cloud VM removes the risk class.

Decision: **Hetzner CX22** (€4.51/mo, 2 vCPU, 4 GB RAM, Ubuntu 24.04). Order at
console.hetzner.cloud, location Falkenstein (de-falkenstein) — geographically
closest to the football-data.org and Brave Search endpoints (lower latency
than US-East), and Hetzner's network has no surprises with Honeycomb's
OTLP HTTPS endpoint.

### One-time provisioning (~5 min)
1. **Buy the VM** in Hetzner Cloud Console → "+ Add Server" → Ubuntu 24.04 →
   CX22 → SSH-key auth (do NOT use password). Note the public IP.
2. **First SSH** to verify access:
   ```bash
   ssh root@<vm-ip>
   ```
3. **Run the bootstrap** (one line):
   ```bash
   wget https://raw.githubusercontent.com/<your-gh-user>/mondial2026/main/infra/bootstrap.sh
   bash bootstrap.sh
   ```
   The script installs Python 3.13, clones the repo into
   `/home/mondial/mondial2026`, creates the venv, installs deps, drops a
   `.env` template, and STOPS — prompting you to fill in the keys.
4. **Fill the .env** with the same secrets from your Mac's `.env`:
   ```bash
   vi /home/mondial/mondial2026/.env
   chmod 600 /home/mondial/mondial2026/.env
   ```
5. **Re-run the bootstrap** to install + enable the systemd unit + nightly
   backup cron:
   ```bash
   bash bootstrap.sh
   ```
   You'll see live JSON logs scrolling — that's the daemon running. Ctrl-C
   exits the tail; the daemon keeps going.

### Day-to-day operations on the VM

| Need | Command |
|---|---|
| Tail live logs | `journalctl -u mondial2026 -f` |
| Service status | `systemctl status mondial2026` |
| Restart after `.env` change | `systemctl restart mondial2026` |
| Stop (e.g. before backup restore) | `systemctl stop mondial2026` |
| Brave/odds_api budget | `PYTHONPATH=. .venv/bin/python tools/brave_quota.py` |
| Full obs audit | `PYTHONPATH=. .venv/bin/python tools/obs_audit.py` |
| Force a backup now | `bash infra/backup.sh` |
| Find the latest backup | `ls -lh store/backup/` |

### Updating the live daemon (safe, atomic, auto-rollback)

You will keep iterating on the code while the daemon runs in production. The
update procedure is one command on the VM, and it's designed to be safe:

```bash
# (on the VM, as root)
/home/mondial/mondial2026/infra/update.sh
```

What it does, in order — each step gates on the previous one:

1. **Refuses if the VM has uncommitted changes.** If you (or anyone) hand-
   edited a file directly on the VM, the script bails so you don't silently
   overwrite work. Investigate, then commit / stash / discard explicitly.
2. **Records the current commit SHA** to `/home/mondial/mondial2026/.last_good_sha`.
   This is the rollback target if the new version misbehaves.
3. **`git fetch` and prints the incoming diff stat** so you can eyeball what's
   about to deploy before any state changes.
4. **`git pull --ff-only`** (no merge commits — refuses if the branches
   diverged, which only happens if someone committed directly on the VM).
5. **Compares `requirements.txt` SHA before/after.** If it changed, runs
   `.venv/bin/pip install -r requirements.txt`. If it didn't, skips pip to
   save the 30-60s install time.
6. **`systemctl restart mondial2026`**, then waits 10 s and runs
   `systemctl is-active`. If the daemon came up → tails 30 journal lines for
   you to eyeball, exits 0.
7. **If the daemon DID NOT come up** → automatic rollback:
   - `git reset --hard <previous SHA>`
   - Restart the daemon
   - Tail the journal so you can see the rollback log lines
   - Exit non-zero with a "fix-it-on-your-Mac" message

So **a broken deploy returns you to the previous working version automatically**
within ~30 seconds. You only have to intervene manually if rollback ALSO fails
(extremely unlikely — means the previous version is also broken, which would
require something other than a code change, e.g. infra drift).

### State preservation across updates

Every piece of tournament state is gitignored, so `git pull` cannot touch it:

| Preserved file | What it holds |
|---|---|
| `.env` | All API keys |
| `store/mondial.db` | Fixtures, predictions, standings, odds snapshots |
| `store/obs.db` | runs ledger (idempotency!), cost ledger |
| `store/elo.json`, `store/fbref_*.json`, `store/results_history.json` | 24h disk caches |
| `store/heartbeat` | Watchdog liveness |
| `store/backup/*.db.gz` | Nightly snapshots |

The runs ledger is what makes restart safe: each `(match_id, window)` pair is
recorded after the card is delivered, so a restart never re-sends one.

### Manual rollback (if you decide later the new version is bad)

```bash
/home/mondial/mondial2026/infra/update.sh --rollback
```

Flips HEAD back to the SHA in `.last_good_sha` and restarts. Use within
~minutes-hours of a deploy if you notice degraded behaviour in the cards
that auto-health-check didn't catch.

### Preview a deploy without applying it

```bash
/home/mondial/mondial2026/infra/update.sh --dry-run
```

Fetches, prints the incoming commit list + diff stat, exits without
modifying anything. Good for "should I push this to prod?" sanity-check.

### Typical development cycle

On your Mac:
```bash
# 1. Make changes, run tests locally
pytest tests/ -q
# (must show 337+ passing — never deploy with red tests)

# 2. Commit + push
git commit -am "fix: <what>"
git push
```

On the VM (in a separate SSH session):
```bash
# 3. (Optional) preview what's coming
/home/mondial/mondial2026/infra/update.sh --dry-run

# 4. Deploy
/home/mondial/mondial2026/infra/update.sh
```

If step 4 ends with "✓ UPDATE OK — daemon running latest", you're done. If
it ends with "FAILED and was rolled back", look at the journal output the
script printed for the error, fix on your Mac, push, re-run step 4.

### Schema migrations (rare but important)

`store/schema.sql` uses `CREATE TABLE IF NOT EXISTS`, so **adding a new
table** is automatically picked up on the next `init_db()` call (i.e.
daemon restart after `update.sh`). **Adding a column to an existing table
is NOT automatic** — `CREATE TABLE IF NOT EXISTS` silently no-ops on a
table that already exists. If you change a column, also commit a one-off
SQL migration script and `sqlite3 store/mondial.db < migration.sql` on the
VM before running update.sh. None of the current code paths require this.

### After the tournament
```bash
# Delete the VM from Hetzner Cloud Console (or hcloud server delete <id>).
# Total cost: 32 tournament days × €4.51/30 ≈ €4.80 for the whole event.
```

### Why this beats the obvious alternatives

| Option | Verdict | Reason |
|---|---|---|
| Mac under launchd | ⛔ | Sleep/close lid kills the daemon — single biggest miss risk. |
| GitHub Actions cron | ⛔ | 5-min minimum races the 6-min T-7m window; SQLite state has no persistent home on a public-repo runner. |
| Render/Railway free | ⛔ | Free background workers idle out (15 min) — defeats "always-on". |
| Oracle Always-Free | 🟡 | Truly free forever but signup is fussy. Fall-back if you don't want to pay anything. |
| **Hetzner CX22** | ✅ | Bulletproof for ~€5 total. Destroy after tournament. |

## Day-9: what the daemon does each tick (Jun-2026 final wiring)

Each `SCHED_POLL_SECONDS` (default 60s) one tick runs the full loop:

1. `_maybe_ingest(now)` — every 30 min, `football_data.refresh()` upserts the
   calendar + tags detonators. Updates utc_kickoff if a game's time changed,
   sets status=FINISHED + scores when a game ends, fills in `home`/`away` for
   knockout TBD rows once the bracket resolves.
2. `_maybe_update_standings()` — every tick, `update_standings(participant="me")`
   re-scores every finished match against your stored predictions. Idempotent
   (Day-5 design); takes a few ms.
3. `_maybe_daily_summary(now)` — at 09:00 Asia/Jerusalem, push a Telegram
   summary (today's games + recent results + your score + Brave/odds budget).
   Tracked via a synthetic match_id `-1` so it never duplicates.
4. `fixtures_fn()` → `upcoming_matches(within_hours=26)` reads SQLite.
5. `due_jobs(matches, now)` returns the (match_id, window) pairs whose trigger
   time has arrived. Catch-up cap 120 min; persistent idempotency via
   `runs.was_handled`.
6. **events_cache batching (Day-9 new)** — if any due job is in
   `{T-60m, T-15m, T-7m}`, call `fetch_all_odds()` ONCE and inject the result
   into every dispatched match. Cuts tournament-wide odds_api credits from
   ~300 → ~120. Fetch failures degrade to per-match (build_card's existing path).
7. Dispatch each due job to the ThreadPoolExecutor (6 workers by default).
   `_dispatched` set + the runs ledger prevent double-firing.
8. `watchdog.beat()` writes the heartbeat file; `watchdog.run_checks()` alerts
   via Telegram if any in-flight run has been stuck > 20 min.

Every external call inside the dispatched pipeline goes through
`obs.external_call(...)` (rate-limit token bucket + Honeycomb span + cost
ledger). The shared bucket means N concurrent matches collectively stay
under each free-tier limit.

## Day-9: Telegram messages you'll receive (the full alert taxonomy)

| Trigger | Title prefix | Source | When |
|---|---|---|---|
| **Card** (the pick itself) | none — formatted card | `core/delivery/base.render_card` via `pipeline.deliver_card` | Each successful match-window: T-24h, T-60m, T-15m, T-7m |
| **Pipeline failure** | `⚠ Pipeline FAILED — <home> vs <away>` | `pipeline.process_match` after all retries | A match build_card raised + retries exhausted. Body: `[stage: <which>] <error>` so you see where it broke. |
| **Delivery failure** | `⚠ Delivery FAILED — <home> vs <away>` | `pipeline.process_match` | Card was computed but no channel accepted it (Telegram down, file write blocked). |
| **Scheduler DOWN** | `⚠ Scheduler DOWN` | `watchdog.check_heartbeat` (called every tick) | Heartbeat file >180s stale → the daemon died and was watching itself? No — this comes from an external cron OR from the next tick if the daemon eventually restarts; mostly the systemd auto-restart kicks in first. |
| **Stuck jobs** | `⚠ Stuck jobs` | `watchdog.check_stuck` (called every tick) | A run started but never finished after 20 min. Body lists match_id + window + started_at. Usually means a hung HTTP call slipped past obs.external_call's rate_timeout. |
| **Daily summary** ☀️ | `☀️ Daily summary — YYYY-MM-DD` | `schedule.daily_summary.send_if_due` | 09:00 Asia/Jerusalem, once per day. Today's games + recent results + your score + budget status. Doubles as a positive heartbeat — if you don't see it, the daemon is dead even when alerts are also broken. |

All go to the **same** `TELEGRAM_CHAT_ID`. They're visually distinct:
- Cards start with `⚽` and are 7-9 lines.
- Alerts start with `⚠` and are 1-3 lines.
- Daily summary starts with `☀️` and is 4-7 lines.

### What it does NOT alert on (and why that's OK)

- **Brave Search / odds_api / api_football single failures** — they're
  swallowed inside `analyze_safe` / `build_card`'s degradation ladder (the
  signal moves to `signals_failed` and the card lands with `⚠news: ...` in
  the Signals line). The CARD itself is the alert.
- **Per-provider rate-limit waits** — the token bucket blocks for up to 30 s;
  beyond that we log + degrade. No Telegram.
- **Ingest failures** — logged + retried next cycle. The watchdog catches it
  via the daily summary going stale (no upcoming-games line will appear).
- **Honeycomb OTLP export failures** — logged only; tracing is observability,
  not a critical path.

### If you stop receiving messages

In priority order:
1. No daily summary at 09:00 → SSH to the VM, `systemctl status mondial2026`.
   If `Active: failed` see `journalctl -u mondial2026 -n 100` for the last
   error. Restart with `systemctl restart mondial2026`.
2. Cards stop landing but daily summary arrives → check `Telegram bot →
   Settings → block`, then `tools/obs_audit.py` to see which provider failed.
3. Random spikes of `⚠ Pipeline FAILED` → check the `failure_reasons` field
   in the most recent rows of `store/mondial.db: predictions`; usually a
   provider rate-limit you can ride out.

