# Handoff to Claude Code — Negev Toto MCP integration

## Goal
We built an MCP connector to our friends' Toto app (`negev-toto.web.app`, a
Firebase Auth + Cloud Firestore app). It already signs in with the user's
email/password and exposes generic Firestore tools. Now: (1) discover the real
collection/document schema, (2) add typed convenience tools, (3) wire the data
into the prediction system. **Do NOT re-architect** — extend what exists.

## What already exists (don't rebuild)
- `integrations/negev_toto_mcp.py` — FastMCP (stdio) server. Auth via Firebase
  Identity Toolkit (`signInWithPassword`) + token refresh. Public apiKey +
  projectId are baked in as defaults; login comes from env `NEGEV_EMAIL` /
  `NEGEV_PASSWORD`. Tools: `toto_ping`, `toto_read_collection`,
  `toto_get_document`, `toto_query`, `toto_patch_document` (writes gated by
  `NEGEV_ALLOW_WRITES=1`). Firestore typed-value encode/decode helpers included.
- `integrations/setup_negev.sh` — installs deps, tests login, registers with
  Claude Desktop.
- `.env.example` — has the NEGEV_* fields.

## Step 1 — discover the schema
Run `toto_ping` (or the read tools) and record the actual top-level collections
and a sample document from each tab:
- Standings (Default + Extended), Matches (with official odds + scores + my pick),
  Broad Bets (futures per player), Side Bets (daily yes/no + community %), and the
  user's notification preferences (Settings).
If `listCollectionIds` is blocked by security rules, get the collection names from
the app's network requests and read them directly with `toto_read_collection`.
Write the discovered schema (collection names + key field names) into
`integrations/SCHEMA_negev.md`.

## Step 2 — add typed tools (in negev_toto_mcp.py)
Map these to the real paths discovered in Step 1. Keep them thin wrappers over the
existing generic read/patch helpers:
- `get_standings(extended=False)` -> [{player, total, group, knockout, side_bets}]
- `get_matches()` -> [{home, away, kickoff, official_odds:{H,D,A}, score, my_prediction, stage}]
- `get_broad_bets()` -> [{player, winner, cinderella, golden_boot, best_player, points}]
- `get_side_bets()` -> [{question, points, lock_time, my_answer, community:{yes_pct,no_pct}}]
- `get_my_preferences()` / `update_preferences(**toggles)`  (write — gated)
- (optional) `submit_prediction(match_id, home, away)` / `submit_side_bet(id, yes)` — only
  after capturing one real write request to confirm the doc path + field shape.
Add unit tests with a mocked Firestore response (no network), like
`tests/test_ingest.py` mocks `requests.get`.

## Step 3 — wire into the prediction system
Add a thin client `core/data/toto.py` that imports the same Firestore read helpers
and exposes:
- `results()`  -> feeds Day-5 scoring (actual scores from the Matches collection).
- `standings()` -> feeds the strategy layer's `standings_context` (replaces the
  manual `standings` table; map players->points).
- `official_odds(match)` -> the EXACT scoring multiplier. In `predict.match_card`,
  prefer these as `scoring_odds` when available (fall back to scraped odds).
Keep all calls inside `obs.external_call("negev_toto", ...)`. Respect the app:
cache reads, low request rate.

## Guardrails
- Never hard-code credentials; env only. Writes stay off unless `NEGEV_ALLOW_WRITES=1`.
- Keep `pytest -q` green; add a test for every new tool/client function.
- This is the user's own account/data — be polite to the backend (cache, throttle).
- **`tournament_id` must be a parameter on every typed tool**, never hard-coded. The
  founder will create the real Mondial 2026 tournament with a fresh id; the
  connector must work against any tournament without code changes.

---

# Progress + remaining work (Jun 2026 — UPDATE AT EACH SESSION)

## ✅ Step 0 — Auth via Google-Sign-In (refresh-token path)

**Done.** The connector reads `NEGEV_REFRESH_TOKEN` from env, primes
`_token['refresh']`, and exchanges it for ID tokens via the Firebase secure-token
refresh endpoint. No password required for Google-only accounts. Verified live:
sign-in OK in ~1 s, 1-hour ID token, refresh token auto-rotates over time.
`.env.example` documents both auth paths (password OR refresh token).

When the token eventually expires (~30 days), the connector raises a clear
re-capture instruction instead of silently falling back to the password path.

## ✅ Step 1 — Schema discovery

**Done.** `integrations/SCHEMA_negev.md` is the canonical reference — read it
before any further work. Key findings:

- 3 top-level collections accessible: `matches`, `users`, `tournaments`.
- `listCollectionIds` is blocked by security rules (403); subcollections were
  found by probing.
- **74 users readable** (paginated) — enables full standings.
- **30 tournament ids referenced** across all users; 3 readable to my account
  ("Ultimate Test", "Second Chance", "Ten Lo"); other 27 are 404. The Mondial
  2026 tournament does **not** exist yet — the founder will create it.
- Per tournament: `broadBets/<userId>`, `sideBets/<id>`, `settings/{broadBets,
  managerTables, checklist, syncLock}` subcollections all reachable.
- **Scoring grids in `settings/managerTables.grids` match our `config/rules.py`
  exactly** (`groupStage`, `round16AndQuarter`, `semiAndFinal`). When the Mondial
  tournament is created, those grids will be the same.
- Per-match user picks and side-bet answers live at a path the security rules
  don't reveal via read — capture them from the app's Network tab on the first
  write attempt and add to schema.

## ✅ Step 2 — Typed convenience tools (DONE 2026-06-07)

**Built and verified live.** 7 typed `@mcp.tool()` functions added to
`integrations/negev_toto_mcp.py`, each a thin wrapper over the existing
generic helpers: `toto_list_tournaments`, `toto_get_standings(tid)`,
`toto_get_matches`, `toto_get_broad_bets(tid)`, `toto_get_side_bets(tid)`,
`toto_get_my_preferences`, `toto_update_preferences(...)` (gated by
`NEGEV_ALLOW_WRITES=1`). Plus `_read_all` helper that paginates Firestore
via `nextPageToken`.

Tests: `tests/test_negev_mcp.py` (20 offline-mocked tests, all green).

Live 2026-06-07: 63 players in Negev Toto 2026
(`n40ykJlOIA9Mg839hz91`), Igor at rank 26, pre-tournament all-zeros.

Also wired in Day 9.6: daily 07:00 IDT sync via
`tools/sync_negev_standings.py` → upserts into our `standings` table for
the strategy layer. Cron line installed by `infra/bootstrap.sh`.

## (kept for reference) ORIGINAL Step 2 spec

Spec below. Every tool MUST accept `tournament_id` as a parameter so the
connector binds to any tournament without code changes. Default `tournament_id`
behavior: read `NEGEV_TOURNAMENT_ID` from env if set, else raise a clear error
asking the user to pass one explicitly. (Adding `NEGEV_TOURNAMENT_ID` to
`.env.example` is part of Step 2.)

Add as `@mcp.tool()` decorated functions in `integrations/negev_toto_mcp.py`,
each a thin wrapper over the existing generic helpers:

```python
@mcp.tool()
def toto_list_tournaments() -> list[dict]:
    """Every tournament id referenced by ANY readable user, with name +
    prizePool if the doc is accessible to us. Lets the user discover the
    Mondial-2026 id once the founder creates it."""

@mcp.tool()
def toto_get_standings(tournament_id: str | None = None,
                       extended: bool = False) -> list[dict]:
    """Sorted standings: [{rank, player, total, direction, broad, exactCount,
    role, uid}]. Filter to users whose tournaments[] contains tournament_id;
    sort by pointsTotal desc, tie-break exactScoreCount desc. extended=True
    keeps the full user doc."""

@mcp.tool()
def toto_get_matches(date_after: str | None = None,
                     status: str | None = None,
                     stage: str | None = None,
                     limit: int = 200) -> list[dict]:
    """Match catalog rows normalized to {match_id, home, away, kickoff_utc,
    stage, status, scoreFullTimeHome/Away, oddsHome/Draw/Away, isDetonator,
    apiFixtureId}. Pass team names through core.data.teams.normalize() so
    they join cleanly with our pipeline. Map raw stage 'Group Stage' →
    'Group', 'Round of 16' → 'R16', etc., into our RULES_STAGE."""

@mcp.tool()
def toto_get_broad_bets(tournament_id: str) -> list[dict]:
    """All futures picks in a tournament: [{userId, displayName, winner,
    cinderella, goldenBoot, bestPlayer, updatedAt}]. Joins on users
    collection for displayName."""

@mcp.tool()
def toto_get_side_bets(tournament_id: str,
                       active_only: bool = False) -> list[dict]:
    """All side-bet questions in a tournament: [{id, question, points,
    startTime, isActive, isLocked, isResolved, correctAnswer, matchId, stage}].
    matchId+stage may be None for free-form (joke) questions."""

@mcp.tool()
def toto_get_my_preferences() -> dict:
    """Read my own user doc and return only the pref_* flags + role + status.
    No network call beyond one toto_get_document('users/<my-uid>')."""

@mcp.tool()
def toto_update_preferences(pref_results: bool | None = None,
                            pref_reminders: bool | None = None,
                            pref_announcements: bool | None = None,
                            pref_broadBets: bool | None = None,
                            pref_sideBets: bool | None = None) -> dict:
    """Patch the pref_* fields on my user doc. Gated by NEGEV_ALLOW_WRITES=1.
    Only fields explicitly passed are sent (None = unchanged)."""

# DO NOT ADD until the doc path + field shape are captured from the
# app's Network tab during a real write — comment this out for now:
# @mcp.tool()
# def toto_submit_match_prediction(tournament_id, match_id, home_goals, away_goals): ...
# @mcp.tool()
# def toto_submit_side_bet_answer(tournament_id, side_bet_id, answer): ...
```

### Tests for Step 2 (tests/test_negev_mcp.py — NEW FILE)

All tests must be **fully offline** — mock `requests.get/post/patch` exactly
like `tests/test_ingest.py` mocks the football-data client. Pattern:

```python
@pytest.fixture
def fake_firestore(monkeypatch):
    """Return a registry the test seeds, mocking _id_token + requests.get."""
    monkeypatch.setattr("integrations.negev_toto_mcp._id_token", lambda: "fake-id-token")
    docs = {}                     # path -> Firestore raw dict
    def _get(url, headers=None, params=None, timeout=None):
        path = url.split("/documents/")[-1]
        ...                       # return registered doc / 404
    monkeypatch.setattr("integrations.negev_toto_mcp.requests.get", _get)
    return docs
```

Required test cases (one per tool, all green on `pytest -q`):

| Test | Asserts |
|---|---|
| `test_list_tournaments_returns_accessible_only` | unioning every user's `tournaments[]` then filtering 404s yields just the ones we can read |
| `test_get_standings_sorts_and_scopes_by_tournament` | users whose `tournaments` lacks the id are excluded; bots optionally excluded; sort order is pointsTotal desc, exactScoreCount desc tie-break |
| `test_get_standings_extended_returns_full_user_doc` | `extended=True` includes raw pref_*, photoURL, createdAt |
| `test_get_matches_normalizes_teams_and_stages` | "Korea Republic" → "South Korea"; "Cape Verde Islands" → "Cape Verde"; "Group Stage" → "Group"; "Round of 16" → "R16" |
| `test_get_matches_passes_status_and_stage_filters` | `status="NS"` → only not-started; `stage="Group"` only group rows |
| `test_get_broad_bets_joins_users_for_displayName` | output rows include displayName for each userId |
| `test_get_side_bets_active_only_filter` | when `active_only=True` only `isActive and not isResolved` rows returned |
| `test_get_my_preferences_returns_only_pref_fields` | no extra fields leaked (pointsTotal, email, etc. excluded) |
| `test_update_preferences_writes_disabled_without_env` | `NEGEV_ALLOW_WRITES=0` → returns the "writes disabled" error, no PATCH attempted |
| `test_update_preferences_skips_unspecified_fields` | only passed kwargs go into the updateMask |
| `test_tournament_id_required_when_not_in_env` | calling `toto_get_standings(None)` with no `NEGEV_TOURNAMENT_ID` raises a clear ValueError |

## ⏳ Step 3 — `core/data/toto.py` (the prediction-system bridge)

**Not yet built.** Once Step 2's typed tools exist, add a thin client at
`core/data/toto.py` that imports them and exposes:

```python
def results(tournament_id: str | None = None) -> list[dict]:
    """Finished matches with scores — feeds Day-5 scoring engine.
    Rows: {match_id, home, away, stage, home_goals, away_goals, status}."""

def standings(tournament_id: str | None = None) -> dict | None:
    """Standings context for the strategy layer — replaces the manual
    `standings` SQLite table. Returns the same shape store.repo.standings_context
    expects: {your_points, leader_points, second_points, games_left} for
    the currently-signed-in user. None if not scoped to a tournament."""

def official_odds(match: dict, tournament_id: str | None = None) -> dict | None:
    """Look up the OFFICIAL scoring odds for a match — preferred over scraped
    odds because these ARE the multiplier the points are computed against.
    Returns {H, D, A} or None. Caller (predict.match_card) falls back to
    scraped odds when None."""

def daily_side_bets(tournament_id: str, day: str | None = None) -> list[dict]:
    """Active, unresolved side-bet questions for a given day — feeds the
    sidebets recommender (core.decision.sidebets) which already produces
    Y/N + EV from per-match models."""
```

Each function wraps every outbound HTTP call in
`obs.external_call("negev_toto", "<endpoint>")` per the project's golden rule.
Add a 24h disk-cache where the data is stable (e.g. tournament settings).

### Step 3 wiring points in the existing prediction system

| Predictor location | Current behavior | New (with Toto wired) |
|---|---|---|
| `core/models/predict.match_card` | Uses `scoring_odds` arg | Caller injects `toto.official_odds(match)` first; falls back to scraped `the-odds-api` only when None |
| `score_match` → `standings` table | Manually populated table | `toto.standings()` becomes the source; remove or deprecate the manual `standings` table population in `tools/dashboard` |
| `core/decision/strategy.recommend_to_win` | Reads from `standings_context` | Source becomes `toto.standings()` |
| `tools/dashboard` | Shows manual standings | Reads `toto.standings()` directly |
| Day 5 results→scoring | football-data results | Optional `toto.results()` as a parallel source for cross-check |

### Tests for Step 3 (tests/test_toto_client.py — NEW FILE)

| Test | Asserts |
|---|---|
| `test_results_returns_finished_only_with_canonical_teams` | non-FT statuses excluded; team names canonicalized |
| `test_standings_context_shape_matches_strategy_layer` | output keys exactly: your_points, leader_points, second_points, games_left |
| `test_official_odds_returns_HDA_dict_or_none` | None on missing odds; never raises |
| `test_official_odds_preferred_over_scraped_in_match_card` | when toto has odds, predict.match_card uses them, not the scraped fallback |
| `test_daily_side_bets_filters_by_day` | only `startTime` matching that day; skipped if locked/resolved |
| `test_obs_external_call_wrapping` | every Toto HTTP call goes through `obs.external_call("negev_toto", ...)` so cost ledger + rate limit apply |

Add `negev_toto` to `config/observability.py::PROVIDER_LIMITS` and `PRICING`
during Step 3 (polite throttle ~10/min, $0 cost).

## Open blockers (must be resolved by the founder, not by us)

1. **Mondial 2026 tournament does not exist.** Once it's created, ask the
   founder for the tournament id, paste into `.env` as `NEGEV_TOURNAMENT_ID=`,
   and the typed tools work end-to-end.
2. **Per-match prediction doc path is unknown.** Open Chrome/Safari DevTools →
   Network tab on `negev-toto.web.app`. Make one prediction in the UI. Capture
   the resulting `PATCH` (Firestore) or `POST` (Cloud Function) request URL +
   body. That's the canonical write path. Add to `SCHEMA_negev.md` and only
   then implement `toto_submit_match_prediction`.
3. **Side-bet answer doc path is unknown.** Same procedure — make one Y/N
   answer in the app, capture the network request, document it, implement.
4. Optional: request `listCollectionIds` read permission from the founder so
   future discovery is deterministic instead of guesswork.

## What we can do today, even without the Mondial tournament

- Step 2 tools work today against ANY of the 3 readable tournaments
  ("Ultimate Test" is the most populated). The standings tool already returns
  74 players' point breakdowns — useful for cross-checking we're computing
  the same totals as the app server-side.
- Step 3's `toto.standings()` can be tested end-to-end against the existing
  tournaments before Mondial goes live.
- The scoring grids in `settings/managerTables` are already verified to match
  our `config/rules.py` — but extend `tests/test_scoring.py` to fetch them
  live (offline-mocked) and compare to our table as a regression net.

## Quick re-discovery commands

```bash
# Re-list every accessible tournament + its count of broadBets/sideBets:
.venv/bin/python -c "
from dotenv import load_dotenv; load_dotenv('.env')
import integrations.negev_toto_mcp as m, json, requests
url = m._base() + '/users'
docs, page = [], {'pageSize': 50}
while True:
    r = requests.get(url, headers=m._headers(), params=page, timeout=20)
    body = r.json(); docs += [m._doc(d) for d in body.get('documents', [])]
    if not body.get('nextPageToken'): break
    page = {'pageSize': 50, 'pageToken': body['nextPageToken']}
tids = {t for u in docs for t in (u.get('tournaments') or [])}
for tid in sorted(tids):
    d = m.toto_get_document(f'tournaments/{tid}')
    if 'error' not in d: print(f'{tid}  name={d.get(\"name\")}')"
```

When the founder creates Mondial 2026, this command will surface the new id —
paste into `.env` and proceed with Step 2 + Step 3 implementation.
