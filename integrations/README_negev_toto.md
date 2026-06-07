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

## The 12 tools

5 generic (low-level) + 7 typed (high-level convenience):

### Generic / discovery
| Tool | Purpose |
|---|---|
| `toto_ping()` | Sign in; list top-level collections (admin endpoint blocked, returns auth UID for verification) |
| `toto_read_collection(c, page_size=50)` | Read a top-level collection — use for ad-hoc schema discovery |
| `toto_get_document(path)` | Read one doc by path |
| `toto_query(c, field, op, value, limit=50)` | Firestore structured-query filter |
| `toto_patch_document(path, fields_json)` | Edit a doc (gated by `NEGEV_ALLOW_WRITES=1`) |

### Typed (recommended for everyday use)
| Tool | Returns | Notes |
|---|---|---|
| `toto_list_tournaments()` | every tournament id + name + prize pool | Sorted by descending pool. Negev Toto 2026 is the real one. |
| `toto_get_standings(tid, extended, include_bots)` | sorted leaderboard `[{rank, player, total, direction, broad, exactCount}]` | Tie-break by exactCount per PDF §19. Bots excluded by default. |
| `toto_get_matches(date_after, status, stage, limit)` | normalized match catalog with our canonical team names + RULES_STAGE labels | Negev's `matches` is global (all leagues); use `stage='Group'` etc. to filter to WC2026 |
| `toto_get_broad_bets(tid)` | every user's futures picks (winner/scorer/cinderella/bestPlayer), joined with displayName | 6 users have submitted as of 2026-06-07 |
| `toto_get_side_bets(tid, active_only)` | daily yes/no shells; `active_only=True` for unresolved+active | 18 shells live (questions empty until founder fills them) |
| `toto_get_scoring_grids(tid)` | the 3 multiplier grids (groupStage/round16AndQuarter/semiAndFinal) | Source of truth for verifying `config/rules.py::SCORE_TABLE` |
| `toto_get_broad_bet_categories(tid)` | full options for the 4 futures categories with current `points` + `isKilled` | Resolves IDs like `team_Portugal` → `{name: "Portugal", points: 39}` |
| `toto_get_match_bets(match_id, tid)` | all picks for ONE match with full breakdown | Lets us audit our score_match() vs Negev's server-side scoring |
| `toto_get_my_bets(tid, limit=200)` | all of MY picks for a tournament | Verifies our daemon's predictions agree with Negev |
| `toto_get_my_preferences()` | my `pref_*` notification flags | One read |
| `toto_update_preferences(...)` | patch my prefs | Gated by `NEGEV_ALLOW_WRITES=1` |

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

## Daily sync to our standings table

`tools/sync_negev_standings.py` runs every morning at 07:00 IDT (cron line
installed by `infra/bootstrap.sh`):

```bash
0 7 * * *  cd /home/mondial/mondial2026 && source .env && PYTHONPATH=. .venv/bin/python tools/sync_negev_standings.py --quiet --telegram
```

What it does:
1. Calls `toto_get_standings()` for `NEGEV_TOURNAMENT_ID`
2. Maps to our `standings` table schema:
   - `directionPoints` → `group_points`
   - `broadBetPoints` → `futures_points`
   - `0` → `knockout_points`
3. Upserts each row (`participant` = Negev `displayName`)
4. Sends a 📊 Telegram summary (top-5 + "Around you" + gap to leader)
5. Wrapped in `obs.external_call("negev_toto", "get_standings")` → traces
   to Honeycomb + counts to cost ledger

CLI flags:
- `--dry-run` — print what would change; touch nothing
- `--telegram` — send the leaderboard to Telegram (default for cron)
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

## Schema reference

Full schema in `integrations/SCHEMA_negev.md`. Highlights:
- 4 top-level collections used: `users`, `tournaments`, `matches`, `bets`
- Sub-collections per tournament: `broadBets`, `sideBets`,
  `settings/{managerTables, broadBets, checklist, syncLock}`
- 36 distinct tournament IDs referenced across users; 3 accessible to my account
- 63 approved players in Negev Toto 2026 as of 2026-06-07

## Telegram messages — full taxonomy after Day-9.6

| Glyph | Sender | When | What |
|---|---|---|---|
| ⚽ | daemon (process_match) | each match window | the pick card |
| ⚠ | daemon | failures | pipeline / delivery / scheduler down |
| ☀️ | daemon (daily_summary) | 09:00 IDT | today's games + recent results + your score + budget |
| 📊 | cron (sync_negev_standings) | 07:00 IDT | live leaderboard top-5 + Igor's rank + gap to leader |

All go to the same `TELEGRAM_CHAT_ID`. Per-chat rate limit is 1/sec —
4 messages/day on quiet days, ~12 on match days. Far under limits.

## Memory + auto-discovery for new sessions

`memory/negev-toto-mcp.md` (in this project's Claude Code memory dir)
records the project's key identifiers: tournament ID, my display name,
the cron schedule, and where to look for full docs. When a new session
opens this directory, the memory loads automatically so Claude knows:

1. Negev MCP is registered locally → I can call `mcp__negev_toto__*`
2. The auto-sync runs at 07:00 IDT → standings table is normally fresh
3. The strategy layer is OFF by default → `STRATEGY_TILT=0` until I flip
4. The natural-language → tool mapping is in this README's table above
