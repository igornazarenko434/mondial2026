# Negev Toto integration — README

Reads (and optionally edits) the friends' Toto app (`negev-toto.web.app`),
a Firebase Auth + Cloud Firestore app. Signs in as YOU and exposes a clean
set of typed tools so Claude can read standings, broad bets, side bets,
matches, and the scoring grids — and (only if you opt in) edit your
preferences.

## Quick start

```bash
# 1. Set env vars in .env (already done — verified 2026-06-07)
NEGEV_REFRESH_TOKEN=<long string starting AMf-...>      # Firebase refresh token
NEGEV_TOURNAMENT_ID=n40ykJlOIA9Mg839hz91                # "Negev Toto 2026"
NEGEV_ALLOW_WRITES=0                                    # writes disabled by default

# 2. Use as a Python library (the sync cron does this; tests use it; CLAUDE.md
#    references it for ad-hoc queries):
from integrations import negev_toto_mcp as m
rows = m.toto_get_standings()            # → top of leaderboard
my_bets = m.toto_get_my_bets()           # → my picks
sb = m.toto_get_side_bets(active_only=True)   # → active yes/no questions

# 3. (Optional) Run as a real MCP server so Claude Desktop / Claude Code can
#    use it natively — already registered in this repo's project config:
.venv/bin/pip install "mcp[cli]"         # one-time, only needed for MCP serving
.venv/bin/python -m integrations.negev_toto_mcp   # stdio transport
```

## The 22 tools (Day-9.6 → Day-9.8)

5 generic (low-level) + 14 typed reads + 3 typed writes.
**4 of the 22 are writes** (`toto_patch_document` generic + 3 typed), all gated by `NEGEV_ALLOW_WRITES=1` (env) + module check + Negev's own Firestore rules.

### Generic / discovery
| Tool | Purpose |
|---|---|
| `toto_ping()` | Sign in; list top-level collections (admin endpoint blocked, returns auth UID for verification) |
| `toto_read_collection(c, page_size=50)` | Read a top-level collection — use for ad-hoc schema discovery |
| `toto_get_document(path)` | Read one doc by path |
| `toto_query(c, field, op, value, limit=50)` | Firestore structured-query filter |
| `toto_patch_document(path, fields_json)` | Edit a doc (gated by `NEGEV_ALLOW_WRITES=1`) |

### Typed reads (recommended for everyday use)
| Tool | Returns | Notes |
|---|---|---|
| `toto_list_tournaments()` | every tournament id + name + prize pool | Sorted by descending pool. Negev Toto 2026 is the real one. |
| `toto_get_standings(tid, extended, include_bots)` | sorted leaderboard `[{rank, player, total, direction, broad, exactCount}]` | Tie-break by exactCount per PDF §19. Bots excluded by default. |
| `toto_get_matches(date_after, status, stage, limit)` | normalized match catalog with our canonical team names + RULES_STAGE labels | **Tournament-scoped (Day-9.6 fix)** — filters by `tournamentId == NEGEV_TOURNAMENT_ID` so no J-League / friendlies leak in. |
| `toto_get_broad_bets(tid)` | every user's futures picks (winner/scorer/cinderella/bestPlayer), joined with displayName | bestPlayer category synthesizes options from `users` collection (it's a META-BET on participants, not football players). |
| `toto_get_side_bets(tid, active_only)` | daily yes/no shells; `active_only=True` for unresolved+active | 18 shells live (questions empty until founder fills them) |
| `toto_get_scoring_grids(tid)` | the 3 multiplier grids (groupStage/round16AndQuarter/semiAndFinal) | **Day-9.7 verified 147/147 cells match `config/rules.py::SCORE_TABLE`** after groupStage column-shift fix. |
| `toto_get_broad_bet_categories(tid)` | full options for the 4 futures categories with current `points` + `isKilled` | Resolves IDs like `team_Portugal` → `{name: "Portugal", points: 39}` |
| `toto_get_match_bets(match_id, tid)` | all picks for ONE match with full breakdown | Lets us audit our score_match() vs Negev's server-side scoring. Used by `tools/post_match_audit.py` (Day-9.8). |
| `toto_get_my_bets(tid, limit=200)` | all of MY picks for a tournament | Verifies our daemon's predictions agree with Negev |
| `toto_get_my_preferences()` | my `pref_*` notification flags | One read |

### Typed reads added (Day-9.6 → Day-9.8)
| Tool | Returns | Notes |
|---|---|---|
| `toto_get_match_details(home, away, tid)` | full per-match view: match row + my pick + friends' picks + applicable exact-score multiplier grid | Convenience for "show me everything about Mexico vs South Africa". |
| `toto_get_side_bets_upcoming(tid)` / `toto_get_side_bets_resolved(tid)` | filtered subsets of side bets | Matches the Negev UI's two panels. |
| `toto_next_match(tid)` | next un-finished match + whether penalties may apply | Used before submitting a result. |

### Typed writes (`NEGEV_ALLOW_WRITES=1` required)
| Tool | Effect | Notes |
|---|---|---|
| `toto_update_preferences(...)` | patch my `pref_*` flags | One PATCH; surface-level |
| `toto_submit_match_prediction(home, away, home_score, away_score, advances_team, match_id, tid)` | create/replace my bet on a single match | **Day-9.6.** Negev's Cloud Function computes points server-side AFTER kickoff. Used 2026-06-07 to save Mexico 2-1. |
| `toto_update_match_result(match_id, home_score, away_score, status, penalty_home, penalty_away, winner_team, tid)` | update the OFFICIAL match result (founder-only in practice) | **Day-9.6.** For knockouts on pens: pass `status='PEN'` + penalty scores + `winner_team`. |

## How natural-language questions map to tools

When asking Claude these typical questions, the routing is:

| Question | Tool |
|---|---|
| "What are the current standings?" | `toto_get_standings()` → display top-N + Igor's rank |
| "Who's leading the pool?" | `toto_get_standings()[0]` |
| "How many points does {X} have?" | `toto_get_standings()` → filter by displayName |
| "What are my current points?" | `toto_get_standings()` → row where player==Igor |
| "What's my gap to the leader?" | `toto_get_standings()` → leader.total - my.total |
| "What are the current side bets?" | `toto_get_side_bets(active_only=True)` |
| "What broad bets do my friends have?" | `toto_get_broad_bets()` |
| "Show me {X}'s futures picks" | `toto_get_broad_bets()` → filter by displayName |
| "What's the next WC match?" | Use our daemon's `repo.upcoming_matches()` (canonical WC source) — Negev's `toto_get_matches` includes all leagues |
| "What's the multiplier for a 2-1 group game?" | `toto_get_scoring_grids()` → `grids.groupStage["2-1"]` OR our `config/rules.py::SCORE_TABLE["group"][(2,1)]` |
| "Who's the favourite to win the tournament?" | `toto_get_broad_bet_categories()` → categories.winner sorted by points |
| "What's been picked for golden boot?" | `toto_get_broad_bet_categories()` → categories.goldenBoot |

## Sync to our standings table — 7 runs/day (Day-9.8)

Canonical crontab `infra/mondial2026.crontab`:

```cron
# Morning summary (Telegram)
0 7 * * *  ...sync_negev_standings.py --quiet --telegram
# Evening match-day syncs (silent; keep DB ≤2h stale)
0 16,18,20,22,0,2 * * *  ...sync_negev_standings.py --quiet
# Post-match audit
0 8 * * *  ...post_match_audit.py --telegram
```

What `sync_negev_standings.py` does:
1. Calls `toto_get_standings()` for `NEGEV_TOURNAMENT_ID`
2. Calls `sync_match_results()` (Day-9.8) — pulls Negev's FT/PEN match outcomes
   into our local `matches` table (status, home/away goals, scoredAt)
3. Calls `sync_standings()` — UPSERTs each row to our `standings` table:
   - `directionPoints` → `group_points`
   - `broadBetPoints` → `futures_points`
   - `knockoutPoints` (when present) → `knockout_points`
4. (If `--telegram`) sends a 📊 leaderboard summary via `delivery.summary()`
5. Wrapped in `obs.external_call("negev_toto", "*")` → Honeycomb traces +
   cost ledger

What `post_match_audit.py` does (Day-9.8):
1. For each FT match, calls `toto_get_match_bets(match_id)` to get Negev's
   awarded points per bet
2. Re-runs our `score_match()` against the same locked T-7m odds + detonator
3. Compares per-bet — if Negev's `processedAt` missing, retries up to 5×30s
4. If any Δ > 0.01 pts → sends 🔍 Telegram with per-match table; silent otherwise

CLI flags (`sync_negev_standings.py`):
- `--dry-run` — print what would change; touch nothing
- `--telegram` — send the leaderboard to Telegram
- `--quiet` — suppress stdout (for cron)
- `--include-bots` — keep role='bot' rows
- `--tournament-id <id>` — override env var

## Consistency audit

`tools/negev_consistency_audit.py` — 4 sections, ~5 Negev reads, read-only:
- §1 PRIZE_LADDER vs Negev (✓ match)
- §2 SCORE_TABLE vs Negev's managerTables (R16/QF/SF/Final ✓; **groupStage has 3 discrepancies**)
- §3 MY_PARTICIPANT in roster (✓)
- §4-6 WC2026 fixtures (skipped until Negev loads them ~24h pre-kickoff)

**Open question for the user**: re-verify §12 group-stage multipliers in
the rules PDF — does 1-0 = 2.25 (our value) or 1.5 (Negev's value)?

## Auth detail

The connector reads `NEGEV_REFRESH_TOKEN` from env (Google-Sign-In path) and
exchanges it via `securetoken.googleapis.com/v1/token` for an ID token. The
refresh token auto-rotates; if it expires (~30 days), the connector raises
a clear "re-capture from DevTools" message. Email/password fallback
(`NEGEV_EMAIL` + `NEGEV_PASSWORD`) works for accounts with email/password
identity but NOT Google-only accounts.

To re-capture the refresh token:
1. Sign in to negev-toto.web.app in your browser
2. DevTools → Application → IndexedDB → firebaseLocalStorageDb →
   firebaseLocalStorage → expand the value → `stsTokenManager.refreshToken`
3. Copy the full string (starts with `AMf-`) into `.env::NEGEV_REFRESH_TOKEN`

## Bots — auto-excluded from our standings

The Negev app has 3 bot accounts (`The Chinchilla`, `The Monkey`, `The Owl`)
for entertainment. They count in the app's visible roster (66 = 63 humans +
3 bots) but **MUST be excluded from our standings tracker** — otherwise
the strategy layer's "gap to leader" math would chase a phantom leader.

Detection (triple-redundant OR — all 3 fields are present today, but
ANY one of them triggers exclusion):

| Signal | Example |
|---|---|
| `role == "bot"` | All 3 bots |
| `isBot == True` | All 3 bots |
| `uid.startswith("bot_")` | `bot_chinchilla` / `bot_monkey` / `bot_owl` |

`toto_get_standings()` excludes them by default. Pass `include_bots=True`
explicitly to see them (useful only for audit / sanity-checks).

## Schema reference

Full schema in `integrations/SCHEMA_negev.md`. Highlights:
- 4 top-level collections used: `users`, `tournaments`, `matches`, `bets`
- Sub-collections per tournament: `broadBets`, `sideBets`,
  `settings/{managerTables, broadBets, checklist, syncLock}`
- 36 distinct tournament IDs referenced across users; 3 accessible to my account
- **73 total users in DB; 66 in Negev Toto 2026 (63 humans + 3 bots — bots filtered out by default)**

## Telegram messages — full taxonomy after Day-9.8

| Glyph | Sender | When | What |
|---|---|---|---|
| ⚽ | daemon (process_match) | each match window (T-24h / T-60m / T-15m / T-7m) | the pick card |
| ⚠ | daemon | failures | pipeline / delivery / scheduler down (uses `delivery.alert()`) |
| ☀️ | daemon (daily_summary) | 09:00 IDT | today's games + recent results + your score + budget (uses `delivery.summary()`) |
| 📊 | cron (sync_negev_standings.py --telegram) | 07:00 IDT | live leaderboard top-5 + Igor's rank + "Around you" window + gap to leader (uses `delivery.summary()`) |
| 🔍 | cron (post_match_audit.py --telegram) | 08:00 IDT | **silent on Δ=0**; sends per-match table only when our `score_match()` differs from Negev's awarded points by >0.01. Day-9.8 addition; retries 5×30s for Negev `processedAt` race conditions. |
| ⚠ | both cron jobs (any failure) | on Negev MCP unreachable | **Day-9.9.** `integrations/negev_alerts.py::alert_failure()` classifies the error (config/auth/rules/network/import/unknown), formats a short body with category + remediation hint + log path, and sends via `delivery.alert()` (gets the ⚠️ prefix). Fires regardless of `--telegram` flag so the 6 silent (`--quiet`) cron runs still warn. Suppress with `--no-alert-on-failure`. |

All go to the same `TELEGRAM_CHAT_ID`. Per-chat rate limit is 1/sec —
2-3 messages/day on quiet days, ~12 on match days, audit silent unless real Δ.
Note: `delivery.summary()` is used for informational ☀️/📊/🔍 to avoid the
⚠️ prefix that `delivery.alert()` prepends.

### Verify the failure-alert path (Day-9.9)

```bash
# On the VM — sends a synthetic ⚠ message to your Telegram. Exits 0 if the
# Telegram round-trip worked. Use after any change to NEGEV_*/TELEGRAM_* env
# vars to confirm the alert wire-up is still live.
sudo -u mondial bash -c 'cd /home/mondial/mondial2026 && set -a && source .env && set +a && PYTHONPATH=. .venv/bin/python tools/sync_negev_standings.py --test-alert'
# Look for: ⚠️ Negev MCP unreachable — unknown   (with "SYNTHETIC TEST" body)
```

Both `sync_negev_standings.py` and `post_match_audit.py` support `--test-alert`.

## Memory + auto-discovery for new sessions

`memory/negev-toto-mcp.md` (in this project's Claude Code memory dir)
records the project's key identifiers: tournament ID, my display name,
the cron schedule, and where to look for full docs. When a new session
opens this directory, the memory loads automatically so Claude knows:

1. Negev MCP is registered locally → I can call `mcp__negev_toto__*`
2. The auto-sync runs at 07:00 IDT → standings table is normally fresh
3. The strategy layer is OFF by default → `STRATEGY_TILT=0` until I flip
4. The natural-language → tool mapping is in this README's table above
