# SERVER.md — Mondial 2026 production server reference

**This file is the canonical operational reference for the always-on Hetzner
server that runs the Mondial 2026 scheduler daemon.** It is written so that
a fresh LLM session (or a fresh human) reading only this file can fully
operate, update, debug, and verify the system without prior context.

If you opened a new chat and pointed an assistant at this repository, the
first file they should read is this one.

---

## 1. What we built and why

The repository builds a **fully autonomous World Cup 2026 predictor** that
delivers per-match picks to Telegram. Source of truth for the architecture
lives in `CLAUDE.md`. Quick recap of what makes a pick:

```
4 signals → 1 score-probability matrix → expected-points optimizer → 1 pick
```

The four signals are:

| Signal | Source | Module |
|---|---|---|
| Team strengths (Dixon-Coles) | martj42 international results CSV (24h cache) | `core/data/results_io.py` + `core/models/dixon_coles.py` |
| Elo ratings | eloratings.net World.tsv (24h cache) | `core/data/soccerdata_io.py` |
| Market odds | the-odds-api (live, T-60m onwards) | `core/data/oddsapi.py` |
| News deltas (lineups, injuries) | api-football + Brave Search + Gemini LLM | `core/data/api_football.py`, `core/data/web_search.py`, `orchestrator/agents/news_agent.py` |

Picks land on Telegram at four windows before each match: **T-24h, T-60m,
T-15m, T-7m**. The T-7m card is the lock — its odds are stored as the
scoring multiplier.

---

## 2. Server identity

| Item | Value |
|---|---|
| Provider | Hetzner Cloud |
| Server type | **CPX22** (2 vCPU AMD, 4 GB RAM, 80 GB NVMe, 20 TB traffic) |
| Cost | $9.49/mo + $0.60 IPv4 = **$10.09/mo** (~$10 for the full tournament) |
| Location | Falkenstein, Germany (de-falkenstein) |
| Image | Ubuntu 24.04 LTS |
| Public IPv4 | `167.233.66.192` |
| Public IPv6 | `2a01:4f8:c015:8eb2::/64` |
| Hostname | `mondial2026` |
| Service user | `mondial` (non-root; daemon runs as this user) |
| Install dir | `/home/mondial/mondial2026` |
| Python | system `python3` (= 3.12 on Ubuntu 24.04) |
| SSH access | `ssh root@167.233.66.192` — key-only, ed25519 with passphrase in macOS keychain |
| TZ | `Asia/Jerusalem` (set by `timedatectl` in bootstrap) |

### Hetzner Cloud Console operations

- **URL**: https://console.hetzner.cloud/
- **Project**: Default
- **Power off / restart / rebuild**: server detail page → tab row near the top
- **Reset root password**: Power tab (NOT the Actions menu — moved depending on UI version)
- **Add SSH key**: left sidebar → Security → SSH Keys → check the box at create-time (rebuild DOES NOT let you change the key)

---

## 3. Filesystem layout on the VM

```
/home/mondial/mondial2026/                  ← repo clone (git pull-able)
├── .env                                    ← secrets (chmod 600 mondial:mondial)
├── .venv/                                  ← Python venv (built by bootstrap.sh)
│   └── bin/python                          ← used by systemd
├── infra/
│   ├── mondial2026.service                 ← systemd unit (symlinked to /etc/systemd/system/)
│   ├── bootstrap.sh                        ← first-time setup
│   ├── update.sh                           ← safe code updates
│   └── backup.sh                           ← nightly SQLite snapshot (via cron)
├── store/                                  ← runtime state (NOT in git)
│   ├── mondial.db                          ← matches, predictions, standings, odds_snapshots
│   ├── obs.db                              ← runs ledger, cost ledger
│   ├── elo.json                            ← 24h cache (eloratings)
│   ├── fbref_2025-2026.json                ← 24h cache (FBref)
│   ├── results_history.json                ← 24h cache (martj42)
│   ├── heartbeat                           ← updated each tick; daemon-liveness check
│   └── backup/                             ← nightly *.db.gz, 7-day rotation
├── reports/                                ← per-match FileReport delivery (legacy fallback)
└── cache/                                  ← reserved for future caches

/etc/systemd/system/mondial2026.service     ← installed by bootstrap.sh (symlink)
/var/spool/cron/crontabs/mondial            ← nightly backup at 03:15 local
```

### What's gitignored (preserved across `update.sh`)

- `.env` — your secrets, NEVER committed
- `store/*.db`, `store/*.json`, `store/heartbeat` — all runtime state
- `store/backup/*` — nightly snapshots

Result: **`git pull` (and therefore `update.sh`) can never touch any
operational state.**

---

## 4. The `.env` file — every variable, what it does, and the default

Located at `/home/mondial/mondial2026/.env`. Mode `600`, owned `mondial:mondial`.

### Required (system can't run without these)

| Variable | What for | Where to get |
|---|---|---|
| `FOOTBALL_DATA_API_KEY` | Fixture calendar + results | https://www.football-data.org/client/register (Free plan: 10/min) |

### Required for full feature set (else the related signal degrades silently)

| Variable | What for | Where to get | Plan / limits |
|---|---|---|---|
| `ODDS_API_KEY` | Bookmaker odds (the scoring multiplier!) | https://the-odds-api.com | Starter $0/mo: **500 credits/month** |
| `API_FOOTBALL_KEY` | Lineups, injuries | https://api-sports.io | Free: **10/min, 100/day** |
| `BRAVE_SEARCH_API_KEY` | News scan for the LLM | https://api-dashboard.search.brave.com | Search plan with $5/mo credit: **1000 free queries/mo** |
| `GEMINI_API_KEY` | News agent LLM (primary in router) | https://aistudio.google.com | Free: **15 RPM, 1500/day** |
| `ANTHROPIC_API_KEY` | Claude Haiku 4.5 (LLM fallback) | https://console.anthropic.com | PAYG ~$1/Mtok in / $5/Mtok out |
| `OPENAI_API_KEY` | gpt-4o-mini (LLM 3rd fallback) | https://platform.openai.com | PAYG |
| `TELEGRAM_BOT_TOKEN` | Card delivery (THE output) | @BotFather on Telegram | Free, 1 msg/sec/chat |
| `TELEGRAM_CHAT_ID` | Your chat for the bot | Send the bot a message, GET `/getUpdates` | — |

### Observability (Honeycomb tracing)

| Variable | What for | Default |
|---|---|---|
| `OTEL_SERVICE_NAME` | Service name in Honeycomb | `mondial2026` |
| `OTEL_TRACES_EXPORTER` | `console`, `otlp`, or `none` | `otlp` (sends to Honeycomb) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP HTTP endpoint | `https://api.honeycomb.io` |
| `OTEL_EXPORTER_OTLP_HEADERS` | Honeycomb API key | `x-honeycomb-team=YOUR_KEY` |

### Scheduler & watchdog (defaults are right for the tournament)

| Variable | Default | Meaning |
|---|---|---|
| `SCHED_POLL_SECONDS` | `60` | How often the daemon recomputes due jobs |
| `SCHED_MAX_WORKERS` | `6` | Concurrent match pipelines (covers 4 simultaneous group kickoffs + 2 slack) |
| `INGEST_EVERY_MIN` | `30` | How often football-data is re-polled (calendar updates) |
| `HEARTBEAT_FILE` | `store/heartbeat` | Daemon-liveness file; touched each tick |
| `WATCHDOG_STUCK_MIN` | `20` | Alert if a run started but never finished within N min |
| `WATCHDOG_HEARTBEAT_MAX_AGE` | `180` | Alert if heartbeat is older than N seconds |
| `LOCAL_TZ` | `Asia/Jerusalem` | Local timezone for display |

### Day-9.5 win-the-pool strategy (default OFF)

| Variable | Default | Meaning |
|---|---|---|
| `MY_PARTICIPANT` | `Igor` | Your display name in the Negev Toto app — used by writer + reader |
| `FRIEND_PARTICIPANTS` | _empty_ | **Day-9.22.** Comma-separated Negev displayNames to also render in every Telegram message (📊 ☀️ ⚽ 🃏). Find exact spelling via `tools/find_member.py <substring>`. Empty → only YOU. |
| `KICKOFF_DELAY_MIN` | `1` | T+N min after kickoff before firing the ⚽ KICKOFF card |
| `KICKOFF_CATCHUP_MIN` | `15` | Catch-up: restart < N min after KO still sends the card |
| `STRATEGY_TILT` | `0` | 0 = pure EV (default). 0.3-0.6 = position-aware. |
| `STRATEGY_TOP_K` | `5` | Pool of EV candidates the tilt may choose from |
| `STRATEGY_SWING` | `6.0` | Estimated swing pts per remaining game |

---

## 5. Provider rate limits — VERIFIED against actual dashboards

`config/observability.py::PROVIDER_LIMITS` is the single source of truth.
Values verified June 2026 against each provider's published page or dashboard:

| Provider | Our config | Published / dashboard | Notes |
|---|---|---|---|
| `football_data` | 10/60s, no daily cap | 10 req/min (Free plan) | exact |
| `odds_api` | 1/2s, 500/month | 500 credits/month (Starter) | exact |
| `api_football` | 10/60s, 100/day | 10/min, 100/day (Free) | exact, verified Jun 2026 dashboard |
| `gemini` | 15/60s, 1500/day | 15 RPM, 1500 RPD (Flash 2.5 free) | exact |
| `claude` | 50/60s, no budget | 50 RPM Tier-1 PAYG, $1/$5 Mtok | exact |
| `openai` | 60/60s, no budget | Tier-1: 500 RPM | conservative |
| `brave_search` | 1/sec, 1000/month | 1 req/sec, $5/1000 + $5 free = 1000/mo | exact |
| `eloratings` | 6/60s, no budget | no published limit (web scrape) | polite self-throttle |
| `martj42` | 6/60s, no budget | GitHub raw: 60/hr anon | polite self-throttle |
| `telegram_bot` | 1/sec, no budget | 1 msg/sec/chat | exact |

**Pricing matrix (`config/observability.py::PRICING`)** drives the cost ledger
`est_cost` column. All providers we use are $0/call except claude and openai.

---

## 6. systemd unit (`infra/mondial2026.service`)

Highlights:

- `Type=simple`, `User=mondial`, `Group=mondial`
- `EnvironmentFile=/home/mondial/mondial2026/.env`
- `ExecStart=/home/mondial/mondial2026/.venv/bin/python -m schedule.runner`
- `Restart=always`, `RestartSec=10`
- `StartLimitBurst=5`, `StartLimitIntervalSec=60` (in `[Unit]`)
- `NoNewPrivileges=true`, `ProtectSystem=strict`, `ProtectHome=read-only`,
  `PrivateTmp=true` (hardening)
- `ReadWritePaths=store cache reports` (only these dirs are writable to the daemon)
- `MemoryMax=512M` (2× measured peak; CPX22 has 4 GB so it's comfortable)

---

## 7. Operations cheat-sheet

All commands assume you SSH'd in: `ssh root@167.233.66.192`.

### Daemon control

```bash
systemctl status mondial2026                  # current state
systemctl restart mondial2026                 # restart (use update.sh instead for code changes)
systemctl stop mondial2026                    # stop (for backup restores)
systemctl start mondial2026                   # start (after stop)
journalctl -u mondial2026 -f                  # live JSON logs
journalctl -u mondial2026 -n 100 --no-pager   # last 100 lines
journalctl -u mondial2026 --since "1 hour ago" | grep '"level":"ERROR"'  # error scan
```

### Deploy code updates (THE canonical flow)

```bash
# After git push from your Mac:
/home/mondial/mondial2026/infra/update.sh           # safe-update with auto-rollback
/home/mondial/mondial2026/infra/update.sh --dry-run # preview only
/home/mondial/mondial2026/infra/update.sh --rollback # manually revert to previous version
/home/mondial/mondial2026/infra/update.sh --force   # update even if a window is mid-flight
```

The script does: clean-tree check → active-worker guard → SHA snapshot for
rollback → fetch + diff stat → `git pull --ff-only` → reinstall deps IF
`requirements.txt` changed → restart → 3-level health check (is-active +
"scheduler started" in journal + zero ERROR lines) → auto-rollback if any
check fails. Full design in `docs/SCHEDULING.md`.

### Audit observability and budgets

```bash
cd /home/mondial/mondial2026
sudo -u mondial bash -c 'set -a && source .env && set +a && PYTHONPATH=. .venv/bin/python tools/obs_audit.py 2>&1 | tail -40'
# Probes every provider once; prints config matrix + ledger usage + Brave quota.
# Burns ~6 free units total (1 Brave + 1 LLM + ~4 small ones). All within budgets.
```

### LLM agent root-cause audit (Day-9.10 — read-only, no API calls)

```bash
sudo -u mondial bash -c 'cd /home/mondial/mondial2026 && set -a && source .env && set +a && PYTHONPATH=. .venv/bin/python tools/llm_audit.py --hours 24'
```

Five sections answer the questions you actually have at 2 AM when a card
landed without news:

1. **CHAIN STATE** — which LLMs *would run right now* for a fresh card, and
   for each chain entry which is bypassed (`no key` / `over budget` / `✓`).
2. **PER-PROVIDER LEDGER** — per gemini / claude / openai over the window:
   call count, ok vs fail, tokens, avg latency, est$, failures broken down
   **by error class** (`RateLimitError × 3`, `APITimeoutError × 1`, etc.).
3. **QUOTA STATE** — used / budget per provider with % and `🛑 OVER` flag.
4. **NEWS CARD AUDIT** — per recent card: `news_provider`, `parse_tier`
   (`strict` / `regex_repair` / `failed` / `never_called`), `fallbacks_used`,
   per-fallback error classes, **and the raw 200-char LLM output when
   `parse_tier == failed`** so you can see exactly what it actually said.
5. **RECENT LLM FAILURES** — last N raw failure rows: timestamp, provider,
   endpoint, error class, message, duration, correlation_id (jump straight
   to Honeycomb with that cid for the full span).

### Brave quota only (quick check)

```bash
sudo -u mondial bash -c 'set -a && source .env && set +a && PYTHONPATH=. .venv/bin/python tools/brave_quota.py'
```

### Calendar & cross-source correlation audit (offline, read-only, ~1 s)

```bash
sudo -u mondial bash -c 'set -a && source .env && set +a && PYTHONPATH=. .venv/bin/python tools/calendar_audit.py'
# 9 sections: match count + stages, groups A-L vs canonical CSV, team-name
# normalization, detonators vs canonical, UTC→Israel time spot-check, what's
# upcoming in 26h, window-firing simulation (when's the FIRST job due?),
# calendar freshness, status distribution + TBD knockouts. Pure SQL + CSV
# diff; no external API calls; doesn't burn any quota.
```

### Negev Toto standings sync (Day 9.6)

Daily sync from the friends' Toto app → our standings table. Runs
automatically at 07:00 IDT via mondial's crontab (installed by
bootstrap). Manual run any time:

```bash
sudo -u mondial bash -c 'set -a && source .env && set +a && PYTHONPATH=. .venv/bin/python tools/sync_negev_standings.py'
# → '✓ 63 players synced. You: rank 26/63 (0 pts; leader Aharony on 0, gap 0)'

# Dry-run (see what would change, write nothing):
sudo -u mondial bash -c 'set -a && source .env && set +a && PYTHONPATH=. .venv/bin/python tools/sync_negev_standings.py --dry-run'
```

Output is consumed by the strategy layer (`store/repo.standings_context`)
and surfaces in the 09:00 daily summary. When the tournament starts, the
sync brings yesterday's points into the system overnight.

### Ad-hoc Negev queries via the typed MCP tools

If you want to inspect the Negev app directly (futures picks, side-bet
questions, leaderboard breakdown), the MCP module exposes 12 tools:

```bash
# Standings for the configured tournament
sudo -u mondial bash -c 'set -a && source .env && set +a && PYTHONPATH=. .venv/bin/python -c "
from integrations import negev_toto_mcp as m
import json
print(json.dumps(m.toto_get_standings()[:10], indent=2))"'

# Every active side-bet question
sudo -u mondial bash -c 'set -a && source .env && set +a && PYTHONPATH=. .venv/bin/python -c "
from integrations import negev_toto_mcp as m
import json
print(json.dumps(m.toto_get_side_bets(active_only=True)[:10], indent=2))"'

# Friends' futures picks (winner/cinderella/scorer)
sudo -u mondial bash -c 'set -a && source .env && set +a && PYTHONPATH=. .venv/bin/python -c "
from integrations import negev_toto_mcp as m
import json
print(json.dumps(m.toto_get_broad_bets(), indent=2))"'
```

### Win-the-pool strategy management

```bash
cd /home/mondial/mondial2026
# Enter the current Negev leaderboard manually
sudo -u mondial .venv/bin/python tools/standings_set.py set "Alice" --group 42.5 --ko 0 --futures 4.2
sudo -u mondial .venv/bin/python tools/standings_set.py set "Bob"   --group 38.0 --ko 0 --futures 7.0
sudo -u mondial .venv/bin/python tools/standings_set.py set "Igor"  --group 35.0 --ko 0 --futures 4.2

# Or bulk-import from JSON:
sudo -u mondial .venv/bin/python tools/standings_set.py import /home/mondial/friends.json

# Inspect
sudo -u mondial .venv/bin/python tools/standings_set.py list
```

Then in `.env`:
```
MY_PARTICIPANT=Igor
STRATEGY_TILT=0.4     # 0 = off; 0.3-0.6 = position-aware
```
And `systemctl restart mondial2026` to pick up the env change.

### Force a manual backup

```bash
sudo -u mondial bash /home/mondial/mondial2026/infra/backup.sh
ls -lh /home/mondial/mondial2026/store/backup/
```

Restore: `gunzip -c store/backup/mondial-YYYY-MM-DD.db.gz > store/mondial.db`
(stop the daemon first).

### Cron — canonical 4 lines (Day-9.8)

Single source of truth: `infra/mondial2026.crontab` (in the repo).
Install / refresh / dedupe with one command:

```bash
sudo -u mondial crontab /home/mondial/mondial2026/infra/mondial2026.crontab
crontab -u mondial -l    # verify exactly 4 active lines (+ comments)
```

| Time IDT | Job | What |
|---|---|---|
| 03:15 | `infra/backup.sh` | Nightly online SQLite snapshot + 7-day rotation |
| 07:00 | `sync_negev_standings.py --quiet --telegram` | Pull Negev results + standings → UPSERT local DB → 📊 Telegram leaderboard |
| 16/18/20/22/00/02 | `sync_negev_standings.py --quiet` | Silent match-day evening syncs (≤2h freshness on points) |
| 08:00 | `post_match_audit.py --telegram` | Cross-check our score_match() vs Negev's awarded points; retries 5×30s if Negev's processedAt missing; sends 🔍 only if any Δ > 0.01 |

Plus the daemon's `_maybe_daily_summary` fires the ☀️ summary at 09:00 IDT
from inside the tick loop (not cron).

### Day-9.8 post-match flow (synchronization end-to-end)

```
T+0:00   Match ends 22:00 IDT
T+0:05   Negev's Cloud Function: matches/{tid_apifid}.status=FT, scores
         each bet, updates each user's pointsTotal (server-side, seconds)
T+0:30   football-data.org publishes status=FINISHED + final goals
T+0:30→+60s
         Our daemon's _maybe_ingest UPSERTs matches table
         (status='FINISHED', goals)
         _maybe_update_standings re-scores via score_match() against
         locked T-7m odds → standings table for me
T+≤2h    Next even-hour cron (16/18/20/22/00/02) runs
         sync_negev_standings.py → sync_match_results() + sync_standings()
         OVERWRITE our standings table with Negev's authoritative numbers
T+next 07:00 IDT
         📊 Telegram standings (now with real numbers)
T+next 08:00 IDT
         🔍 Telegram audit — silent if our calc matches Negev's, alert
         otherwise (with per-match table showing Δ)
T+next 09:00 IDT
         ☀️ Daily summary reflects everything
```

### Day-9.6 Negev MCP integration (22 tools)

`integrations/negev_toto_mcp.py` exposes 22 `@mcp.tool()` functions: 5 generic
(low-level Firestore CRUD), 14 typed reads, 3 typed writes. The 4 mutating
tools (generic `toto_patch_document` + 3 typed) are triple-gated by
`NEGEV_ALLOW_WRITES=1`. Routing-by-natural-language table in
`integrations/README_negev_toto.md`. Live smoke-test:

```bash
sudo -u mondial bash -c 'cd /home/mondial/mondial2026 && set -a && source .env && set +a && PYTHONPATH=. .venv/bin/python tools/verify_negev_live.py'
```
14 checks, exit 0 if all pass. Includes scoring-grid cross-check vs config/rules.py
(Day-9.7 fix verifies 147/147 cells now match Negev).

### Inspect the live system via SQL

```bash
# Calendar — next 10 games
sudo -u mondial sqlite3 /home/mondial/mondial2026/store/mondial.db "
  SELECT match_id, utc_kickoff, stage, home, away, status, detonator
  FROM matches WHERE status IN ('SCHEDULED','TIMED') ORDER BY utc_kickoff LIMIT 10"

# What's already been picked
sudo -u mondial sqlite3 /home/mondial/mondial2026/store/mondial.db "
  SELECT match_id, window, created_at, pick_dir, pick_h, pick_a, expected_points
  FROM predictions ORDER BY created_at DESC LIMIT 10"

# Standings
sudo -u mondial sqlite3 /home/mondial/mondial2026/store/mondial.db "
  SELECT participant, group_points, knockout_points, futures_points,
         (group_points + knockout_points + futures_points) AS total
  FROM standings ORDER BY total DESC"

# Cost ledger — per-provider totals
sudo -u mondial sqlite3 /home/mondial/mondial2026/store/obs.db "
  SELECT provider, SUM(units) AS units, SUM(tokens) AS tokens, COUNT(*) AS calls
  FROM api_calls GROUP BY provider ORDER BY units DESC"

# Runs ledger — recent dispatch outcomes
sudo -u mondial sqlite3 /home/mondial/mondial2026/store/obs.db "
  SELECT match_id, window, status, card_delivered, fell_back, started_at, detail
  FROM runs ORDER BY started_at DESC LIMIT 20"
```

### Honeycomb

- URL: https://ui.honeycomb.io/
- Dataset: `mondial2026`
- Filter spans by `correlation_id = "match-<mid>-T-7m"` to see one job's
  end-to-end timing (DC fit → news agent → odds → blend → deliver).

---

## 8. Telegram alerts you'll receive

| Glyph | Title | When |
|---|---|---|
| ⚽ | (the formatted card) | each match window: T-24h, T-60m, T-15m, T-7m |
| ⚠ | `Pipeline FAILED — H vs A` | match-window pipeline raised + retries exhausted |
| ⚠ | `Delivery FAILED — H vs A` | card built but no channel accepted it |
| ⚠ | `Scheduler DOWN` | heartbeat >180s stale (daemon died) |
| ⚠ | `Stuck jobs` | a started run never finished within 20 min |
| ☀️ | `Daily summary — YYYY-MM-DD` | 09:00 Asia/Jerusalem — positive heartbeat + day-at-a-glance |

If you DON'T see the ☀️ at 09:00, SSH in and check `systemctl status mondial2026`.

---

## 9. Common problems and fixes

### Daemon won't start after update

`update.sh` auto-rolls back. If you still need to investigate:
```bash
journalctl -u mondial2026 -n 100 --no-pager
```
Common causes: typo in `.env`, missing required env var, broken migration. Fix
on Mac → push → re-run `update.sh`.

### "No games today" but matches are scheduled

Check that ingest is working:
```bash
journalctl -u mondial2026 --since "1 hour ago" | grep "calendar"
```
Should see `calendar refresh: 104 matches` lines every 30 min. If not:
- Verify `FOOTBALL_DATA_API_KEY` is valid (`grep FOOTBALL .env`)
- Check the daily summary's Budget line — football-data has no daily cap so
  it's always fine, but a 403 from a bad key would surface as errors in journal

### `was_handled` filtering out a job that didn't actually deliver

Rare race: a worker was killed between `runs.start` and Telegram delivery.
Fix:
```bash
sudo -u mondial sqlite3 /home/mondial/mondial2026/store/obs.db "
  DELETE FROM runs WHERE match_id=<mid> AND window='<window>' AND card_delivered=0"
```
The daemon will pick it up on the next tick.

### Strategy tilt isn't activating

Verify:
1. `MY_PARTICIPANT` matches a participant row exactly (`tools/standings_set.py list` shows ←you)
2. At least 2 rows in standings (need a leader to compare against)
3. `STRATEGY_TILT > 0` (`grep STRATEGY .env`)
4. `systemctl restart mondial2026` after .env changes

Then look for `strategy tilt re-picked` in the journal.

### Brave / odds quota approaching limit

```bash
sudo -u mondial bash -c 'set -a && source /home/mondial/mondial2026/.env && set +a && PYTHONPATH=/home/mondial/mondial2026 .venv/bin/python /home/mondial/mondial2026/tools/brave_quota.py'
```
Brave has a hard `BRAVE_BUDGET_BRAKE_FRACTION=0.90` cutoff — at 90% used, news
calls silently no-op (cards still land, just without news signal).

---

## 10. Tournament timeline (autopilot from here)

- **Now → 2026-06-10 ~22:00 Israel**: daemon idle-ticks. Daily summary every morning at 09:00 Israel.
- **2026-06-10 22:00 Israel** (=19:00 UTC): first T-24h card lands (Mexico vs South Africa).
- **2026-06-11 21:00 / 21:45 / 21:53 Israel** (=18:00 / 18:45 / 18:53 UTC): T-60m / T-15m / T-7m for opener.
- **2026-06-11 21:59 Israel**: 🚨 **manual deadline — enter Day-7 futures (Portugal/Uzbekistan/Mbappé) in Negev Toto app**
- **2026-06-11 22:00 Israel** (=19:00 UTC): kickoff. Result ingested ~2h later; standings update within 30 min.
- **2026-06-27 (approx)**: group stage ends. Consider activating `STRATEGY_TILT=0.4` if behind.
- **2026-07-19**: Final. Run `tools.calibrate.run()` afterwards for post-tournament weights tune.
- **After Final**: destroy the Hetzner server (~€5 total for the event).

---

## 11. If you're a new LLM session reading this

To pick up the project:

1. Read this file (you just did).
2. Read `CLAUDE.md` for build-order and architecture context.
3. Read `docs/SCHEDULING.md` for daemon internals + update flow.
4. Read `docs/STRATEGY.md` for win-the-pool details.
5. Read `docs/OBSERVABILITY.md` for trace/cost details.

To make a change:

1. On the user's Mac: edit code, run `pytest tests/ -q` (must show 466+ passing).
2. Commit + push to `main` on GitHub.
3. On the VM: `/home/mondial/mondial2026/infra/update.sh`.
4. Verify with `journalctl -u mondial2026 -f` and the queries in §7.

To debug a production issue:

1. `systemctl status mondial2026` — is it running?
2. `journalctl -u mondial2026 --since "1 hour ago" | grep -iE "error|warning"` — what broke?
3. SQL queries in §7 — what's the system's view of state?
4. Honeycomb — what was the span timeline?
5. If all else fails: `infra/update.sh --rollback`.
