# Negev Toto Firestore Schema (discovered Jun 2026)

Live-discovered against the production database for the friends' Toto app
(`negev-toto.web.app`, Firebase project `negev-toto`) using the MCP connector
in this directory. Sign-in tested working via the Firebase **refresh-token**
path (Google Sign-In capture). Project apiKey + projectId verified against
the public `negev-toto.web.app/__/firebase/init.json`.

`listCollectionIds` is blocked by security rules (403). All names below were
either reached by direct read or inferred from cross-references in returned
documents. Subcollections we couldn't probe positively are marked **unknown**.

---

## Auth

| Path | Status |
|---|---|
| `signInWithPassword` (email/password) | only works if the user has set a Firebase email/password identity. Default account `<your-email>@example.com` is a **Google-Sign-In-only** account → returns `INVALID_LOGIN_CREDENTIALS`. |
| `securetoken.googleapis.com/v1/token` (refresh-token flow) | ✅ working. Seed once from browser DevTools → `IndexedDB / firebaseLocalStorageDb / firebaseLocalStorage / stsTokenManager.refreshToken` → paste into `NEGEV_REFRESH_TOKEN`. Auto-refreshes for ~30 days. |

Connector primes `_token['refresh']` from `NEGEV_REFRESH_TOKEN` and falls
back to password if both are set.

---

## Top-level collections

| Collection | Access | Purpose |
|---|---|---|
| `matches` | read ✅ | Global football-match catalog (~thousands of docs across many tournaments / leagues). Past tournaments (Qatar 2022) + current friendlies + Swedish Allsvenskan + Ligue 1 + J-League + Europa League. **The Mondial 2026 fixtures are NOT in here yet** — they'll be inserted by the founder when the tournament goes live. |
| `users` | read ✅ | **74 readable docs** (verified via pagination — full visible roster). Security rules let any signed-in player see every other player's profile (needed for standings). Holds the points fields + notification preferences. |
| `tournaments` | document read ✅ (collection list = blocked by rules) | One doc per pool/game. **30 distinct ids referenced across users**, **3 readable** to my account (the ones I'm a member of); the others 404 with no read permission. |

### `matches/<apiFixtureId>` (sample: `matches/855734` — Senegal v Netherlands 2022)

```jsonc
{
  "apiFixtureId": 855734,           // also the doc id (string in path, int in field)
  "homeTeam": "Senegal",            // ← raw, NOT normalized to our canonical
  "awayTeam": "Netherlands",
  "homeLogo": "https://media.api-sports.io/...",
  "awayLogo": "...",
  "date": "2022-11-21T16:00:00+00:00",
  "stage": "Group Stage - 1",       // ← free-form string, NOT our RULES_STAGE
  "status": "FT",                   // FT|NS|PEN|... (api-football status codes)
  "isDetonator": false,
  "exactScoreMultiplier": 1,
  // official odds — the SCORING MULTIPLIER under Toto rules:
  "oddsHome": null, "oddsDraw": null, "oddsAway": null,
  "oddsSource": "api",              // "api" or manual override
  // final score (current + full-time + penalty shootout):
  "goalsHome": 0, "goalsAway": 2,
  "scoreFullTimeHome": 0, "scoreFullTimeAway": 2,
  "scorePenaltyHome": null, "scorePenaltyAway": null
}
```

### `users/<uid>` (full field union across all 74 readable docs)

```jsonc
{
  "uid": "<your-firebase-uid>",
  "email": "<your-email>@example.com",
  "displayName": "Igor",
  "photoURL": "https://lh3.googleusercontent.com/.../a/...",
  "role": "player",                 // "player" | "bot" | (?)"admin"
  "status": "approved",
  "createdAt": "2026-05-30T17:30:47.278Z",
  "tournaments": ["KTOgXQp1bLSEiXengGUl", "B0Bzf02JUPWx51BWBFpa"],
  "isBot": false,                   // present on bot rows (e.g. "The Chinchilla")
  // ↓ STANDINGS INPUTS (driven server-side; recomputed by app):
  "pointsTotal": 0,                 // total rank
  "directionPoints": 0,             // 1X2 + exact-score scoring (group + KO combined)
  "broadBetPoints": 0,              // futures (winner/Cinderella/golden-boot/best-player)
  "exactScoreCount": 0,             // tie-breaker — #exact-score hits (PDF §19)
  // ↓ SETTINGS tab — notification toggles:
  "pref_results": true,
  "pref_reminders": true,
  "pref_announcements": true,
  "pref_broadBets": true,
  "pref_sideBets": true
}
```

> **Standings note:** points fields look GLOBAL (not per-tournament) on the
> user doc. To get a *Mondial-scoped* standings table, filter users whose
> `tournaments` array contains the Mondial tournament id; sort by
> `pointsTotal` desc, tie-break by `exactScoreCount` desc (matches PDF §19).

### `tournaments/<tid>` (3 accessible right now)

```
277y8y3DQpzKIhGLp9rw  "Ultimate Test"   prizePool=1000   (most populated)
B0Bzf02JUPWx51BWBFpa  "Second Chance"   prizePool=1000   (active joke pool)
zTXREWc1Gm9xRDTWqxOj  "Ten Lo"          prizePool=3500   (larger purse)
```

Sample doc shape (`tournaments/B0Bzf02JUPWx51BWBFpa`):

```jsonc
{
  "name": "Second Chance",
  "createdAt": "2026-06-04T13:31:45.614Z",
  "lastRankSnapshot": "2026-06-05",
  "settings": {
    "totalPrizePool": 1000,
    "prizePercentages": [25, 20, 15, 12, 10, 8, 5, 3, 2, 0],
    "prizeDistribution": [250, 200, 150, 120, 100, 80, 50, 30, 20],
    "kodBonuses": [15, 12, 10, 7, 5],
    "showPrizes": false,
    "showEndGameColumns": false,
    "isDirectionsEnabled": true,
    "broadKingPrize": 523
  }
}
```

> Note: the `tournaments/<tid>.settings` *object on the parent doc* is a quick
> summary; the **full** scoring/futures config lives in subcollection docs
> under `tournaments/<tid>/settings/` (see below).

---

## Subcollections under `tournaments/<tid>` (verified)

| Subcollection | Doc id pattern | Purpose |
|---|---|---|
| `broadBets` | `<userId>` (one doc per player) | A player's futures picks for this tournament |
| `sideBets` | `sb_YYYY-MM-DD` | Daily side-bet questions + outcome |
| `settings` | `broadBets` / `managerTables` / `checklist` / `syncLock` | Per-tournament configuration — futures categories, scoring grids, sync state |

### `tournaments/<tid>/broadBets/<userId>` (futures pick per player)

```jsonc
{
  "userId": "0gKs64hAsqTjajfEm7vDnvcOAKX2",
  "updatedAt": "2026-06-04T14:06:26.703Z",
  "selections": {
    "winner":    "team_Cyprus",                            // id from settings/broadBets categories
    "cinderella":"team_Burundi",
    "goldenBoot":"1780580161396",                          // numeric player id
    "bestPlayer":"roster_xwzwsQmFg4dGEUnf98KKO1qVt2A3"     // roster id (player)
  }
}
```

### `tournaments/<tid>/sideBets/<id>`

Doc id pattern is `sb_YYYY-MM-DD` (Second Chance) **or** an autogenerated id
(Ultimate Test). Two field variants seen — the second adds match linkage:

```jsonc
// variant 1: free-form personal-joke question (Second Chance)
{
  "question": "Alfi Quits under 54 y/o",
  "points": 1,
  "startTime": "2026-06-04T19:00:00.000Z",
  "isActive": true,
  "isLocked": false,
  "isResolved": false,
  "correctAnswer": null,                // boolean once resolved (Y/N)
  "manuallyUnlocked": false,
  "createdAt": "2026-06-04T13:36:46.928Z"
}

// variant 2: MATCH-LINKED side bet (Ultimate Test) — these are the ones
// the prediction system can model directly via core.decision.sidebets
{
  "question": "Will there be 3 or more goals today?",
  "points": null,                        // may be set per question
  "stage": "Group Stage",                // optional — bracket scope
  "matchId": null,                       // optional — apiFixtureId when bet is per-match
  "startTime": "2026-06-06T05:00:00+00:00",
  "isActive": true,
  "isLocked": true,
  "isResolved": false,
  "correctAnswer": null,
  "manuallyUnlocked": false,
  "createdAt": "2026-06-01T09:27:07.293Z"
}
```

### `tournaments/<tid>/settings/broadBets` (futures options + payouts)

Holds the per-tournament list of choices for each futures category
(winner / cinderella / golden boot / best player), with a fixed `points`
payout per option (matches our rules: "Spain 20 ... USA 170" pattern).

```jsonc
{
  "isPublished": true,
  "isLocked": true,
  "categories": [
    { "id": "winner", "title": "Tournament Winner", "options": [
        { "id": "team_Afghanistan",    "name": "Afghanistan",     "points": 10, "isKilled": false },
        { "id": "team_AlbaniaU19",     "name": "Albania U19",     "points": 10, "isKilled": false },
        { "id": "team_Cyprus",         "name": "Cyprus",          "points": 10, "isKilled": false },
        // … (this tournament's list is silly; Mondial 2026's list will be the 48 real teams)
    ]},
    // also: cinderella, goldenBoot, bestPlayer (same shape)
  ]
}
```

### The 4 broad-bet categories — what each actually means

Don't conflate the IDs — `bestPlayer` is a meta-bet on the leaderboard, not a
football-player pick:

| Category id | Real name in UI | Bet on | Options come from |
|---|---|---|---|
| `winner` | Tournament Winner | Which **team** wins the WC | 48 team options in `settings/broadBets.categories[winner]` |
| `cinderella` | Cinderella Team | Which underdog **team** goes furthest | 48 teams (only 11 have non-zero points) in same doc |
| `goldenBoot` | Golden Boot | Which **player** scores most goals | 19 strikers in same doc |
| **`bestPlayer`** | **Best Placed Player** | **Which PARTICIPANT (friend!) finishes highest in the pool** | The doc only stores 1 placeholder. **The Negev app dynamically builds this dropdown from the `users` collection** (all approved humans in this tournament). Our `toto_get_broad_bet_categories` does the same synthesis client-side. |

### Bot accounts (3 known, must be excluded)

The Negev Toto app has 3 bot players for entertainment — their position is
decorative and MUST NOT count in our standings tracker (otherwise the
strategy layer's leader-gap math would be wrong).

| displayName | uid | role | isBot | pointsTotal (2026-06-07) |
|---|---|---|---|---|
| The Chinchilla | `bot_chinchilla` | `bot` | `true` | 4.3 |
| The Monkey | `bot_monkey` | `bot` | `true` | 0 |
| The Owl | `bot_owl` | `bot` | `true` | 0 |

Triple-redundant detection (`integrations/negev_toto_mcp.py::_is_bot`):
- `role == "bot"`
- `isBot == True`
- `uid.startswith("bot_")`

OR'd together so a bot missing one signal is still caught.
`toto_get_standings()` excludes bots by default (set `include_bots=True`
to see them — useful for the audit tool that wants to verify count
math: 66 app participants = 63 humans + 3 bots).

### `tournaments/<tid>/settings/managerTables` (the EXACT-SCORE GRIDS)

The exact-score multiplier table, per stage bracket. Grid keys: `groupStage`,
`round16AndQuarter`, `semiAndFinal`.

**Cross-verified against our `config/rules.py::SCORE_TABLE`
(2026-06-07, live `n40ykJlOIA9Mg839hz91`, post Day-9.7 fix):**
- `groupStage`: **all 49 cells match** ✓
- `round16AndQuarter`: **all 49 cells match** ✓
- `semiAndFinal`: **all 49 cells match** ✓

**Day-9.7 fix history**: an initial audit (2026-06-07) caught 3 cells in
the groupStage table that disagreed with Negev's server-side scorer
(1-0, 2-0, 3-0 — all "clean-sheet home wins"). Our `_GROUP[0]` was
`[2.75, 2.25, 3.25, 4.5, ...]` but Negev's authoritative grid uses
`[2.75, 1.5, 2.25, 3.25, 4.5, ...]`. The previous values came from a
misread of the PDF row (off by one column). After Day-9.7's commit the
table reads `[2.75, 1.5, 2.25, 3.25, 4.5, 4.5, 4.5, 4.5]` — verified by
`tools/negev_consistency_audit.py` showing 0 differences across 147
cells (3 grids × 49 each).

Internal consistency Negev uses (a sanity check that the new values are
right):
  1-0 ↔ 2-1 = **1.5**   (same difficulty — low-scoring home win)
  2-0 ↔ 3-1 = **2.25**
  3-0 ↔ 4-1 = **3.25**

Worked PDF examples still hold:
  France 2-1, group, odds 2.0 → 1.5 × 2.0 = **3.0**  ✓
  Draw 1-1, group, odds 2.5  → 2.25 × 2.5 = **5.625** ✓
  Final 2-2, draw odds 2.5   → 5 × 2.5 = **12.5** ✓

```jsonc
{
  "grids": {
    "groupStage":        { "0-0": 2.75, "1-0": 1.5,  "1-1": 2.25, "2-1": 1.5,  "2-2": 2.75, "6+-6+": 8.25 },
    "round16AndQuarter": { "0-0": 3.75, "1-0": 2.25, "1-1": 3,    "2-1": 2.25, "2-2": 3.75, "6+-6+": 8.25 },
    "semiAndFinal":      { "0-0": 5,    "1-0": 3,    "1-1": 4,    "2-1": 3,    "2-2": 5,    "6+-6+": 11   }
  }
}
```

> Verification quick-reference: France 2-1 (group) → 1.5 × 2.0 odds = **3.000**
> ✓ matches PDF worked example. 1-1 (group) → 2.25 × 2.5 = **5.625** ✓.
> Final 2-2 → 5 × 2.5 = **12.5** ✓.

### `tournaments/<tid>/settings/checklist`

App-internal checklist of "have they been edited yet" gates. Probably not
useful for the prediction system.

```jsonc
{ "pre_edit_broad": true, "pre_edit_exact": true }
```

### `tournaments/<tid>/settings/syncLock`

```jsonc
{ "lastSyncAt": "2026-06-05T15:00:33.341Z" }
```

---

## Subcollections NOT FOUND (probed, all returned 0 docs OR 403)

These were tried with both top-level and tournament-scoped paths; none
returned any data for the user/tournament I have access to:

- `tournaments/<tid>/predictions`
- `tournaments/<tid>/userPredictions`
- `tournaments/<tid>/picks`
- `tournaments/<tid>/userPicks`
- `tournaments/<tid>/matchPicks`
- `tournaments/<tid>/entries`
- `tournaments/<tid>/users`
- `tournaments/<tid>/matches`
- `tournaments/<tid>/scoring`
- `tournaments/<tid>/playerStats`
- `users/<uid>/predictions`
- `users/<uid>/picks`
- `users/<uid>/sideBets` (answers)
- `users/<uid>/broadBets`
- `users/<uid>/settings`
- `matches/<mid>/predictions`
- top-level `predictions`, `picks`, `userPredictions`, `matchPredictions`, `entries`, `bets`

**Likely interpretation:** per-match 1X2 + exact-score picks are stored under
a path the security rules only expose at write-time, OR they're written into
a path I haven't guessed. The next thing to do — once the Mondial 2026
tournament is created — is to open the app's Network tab while making a pick,
capture the exact `PATCH` / `POST` URL, and add that name here.

Side-bet answers (which side bet the user picked Y/N) are presumably also in
this hidden location.

---

## What is NOT yet in the database

- **The Mondial 2026 tournament itself.** My user has two tournament IDs:
  - `B0Bzf02JUPWx51BWBFpa` = **"Second Chance"** (active joke pool — Cyprus, Burundi, "Alfi quits under 54", etc.)
  - `KTOgXQp1bLSEiXengGUl` = **404 not found** (deleted, or never created, or no read access).
  - Query for tournaments named "Mondial 2026" / "Mondial" / "World Cup" / "WC 2026" / "FIFA 2026" — **all empty**.
- **The 104 WC 2026 fixtures.** The `matches` collection currently holds Qatar 2022 + 2026 friendlies + league fixtures (Swedish Allsvenskan, Ligue 1, J-League, Europa League). No `stage="Group"` rows for 2026-06-11 onward.
- **Per-player predictions for any tournament we can see.**

This means **Step 3 (wire into the prediction system) cannot yet pull
real Mondial scores / odds / picks from this DB** — the data doesn't exist
yet. Step 3 will be implementable once an admin creates the tournament and
ingests the 104 fixtures (presumably closer to 11 Jun 21:59 lock).

---

## Mapping to the prediction-system needs (Step 3 plan)

When the Mondial 2026 tournament exists:

| Predictor field | Firestore source |
|---|---|
| `match.home/away/stage/detonator/kickoff` | `matches/<apiFixtureId>` (after normalizing team names via `core.data.teams.normalize` and stage via a new map for "Group Stage", "Round of 16" → our `RULES_STAGE`) |
| `scoring_odds = {H, D, A}` (the EV multiplier) | `matches/<apiFixtureId>.oddsHome / oddsDraw / oddsAway`. **Prefer this over the-odds-api** when present — it's literally the multiplier used in scoring. |
| Actual final score (for Day-5 scoring) | `matches/<apiFixtureId>.scoreFullTimeHome / Away` (+ penalty shootout fields for KO games) |
| Standings (per player) | `users/*.pointsTotal/directionPoints/broadBetPoints` — already separated by group/knockout in the points columns; populate `standings` table from this. |
| My futures picks (lock by 11.06 21:59) | write to `tournaments/<tid>/broadBets/<my-uid>.selections` |
| Daily side bet — current questions + locks | `tournaments/<tid>/sideBets/sb_YYYY-MM-DD` |
| My side bet answer | **unknown path** — capture from app network tab on first answer |
| My per-match 1X2 + exact pick (write) | **unknown path** — capture from app network tab on first pick |
| Tournament-specific scoring rules | `tournaments/<tid>/settings/managerTables` (exact-score grids) + `settings/broadBets.categories[].options[].points` (futures payouts) — both match our PDF |

---

## Open items / next discovery steps

1. **Confirm with the friends when the Mondial 2026 tournament will be created** in this app. Without it, Steps 2 + 3 only have skeletal data to work against.
2. **Capture one real `PATCH` / `POST` from the app's Network tab** when making a per-match pick and a side-bet answer — this gives us the doc path + field names for the only two unknowns above.
3. Optionally: ask the admin to grant `listCollectionIds` permission so future discovery is deterministic instead of guesswork.

---

## How to refresh this schema

```bash
.venv/bin/python -c "
from dotenv import load_dotenv; load_dotenv('.env')
import integrations.negev_toto_mcp as m, json
# any of: toto_ping / toto_read_collection / toto_get_document / toto_query
print(json.dumps(m.toto_read_collection('tournaments/B0Bzf02JUPWx51BWBFpa/sideBets'), indent=2))
"
```

Avoid writes (`toto_patch_document`) unless `NEGEV_ALLOW_WRITES=1` and you
have specifically confirmed the doc path + fields.
