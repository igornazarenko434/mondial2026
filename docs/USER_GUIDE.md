# How you actually use this system (day-to-day)

## The short version
This system is your **advisor**, not your bookmaker. It does **not** place bets in
your friends' Toto app. For each match it sends you a recommendation a few minutes
before kickoff; **you read it and type that pick into the friends' app yourself**,
and log it in the spreadsheet. That's the whole loop.

## Do you need a frontend / website? No.
For a single user, building a web app would be wasted effort. The best-practice,
minimum-effort "interface" is **push notifications + generated files**:

1. **Per-game card → your phone (Telegram).** ~7 minutes before each kickoff you
   get a message like:
   > **Norway vs France** (Group) ⚡ DETONATOR x2
   > Locked odds — H 4.20 / X 3.60 / A 1.85
   > Model — H 20% / X 25% / A 54%
   > ► Pick direction: Draw  ► Exact score: 1-1 (likeliest 1-1)
   > Expected points ≈ 2.92
   > • Norway may rest starters; Mbappé starts.

   You read it, enter that pick in the Toto app before it locks, done.
2. **Browsable record → files.** Every card is also written to `reports/` (a
   per-match `.md` and an appended `feed.md`), so you can scroll the history any
   time with no app.
3. **One-screen overview → optional static dashboard.** Run
   `python -m tools.dashboard` to generate `reports/dashboard.html` (upcoming
   picks, standings, run health, quota usage). Open the file — no server needed.

That's the recommended setup. A live web UI is optional and only worth it if you
want a shared screen for the whole group (see "If you ever want more" below).

## What you fill in vs. what's automatic
| You do | The system does |
|---|---|
| One-time: add free API keys to `.env`; set futures picks before 11.06 21:59 | Pulls fixtures, stats, odds, lineups automatically |
| Read each card; enter that pick in the Toto app | Computes the EV-optimal pick and sends the card at T-7m |
| After a game, log the actual score (spreadsheet or `results`) | Scores it, updates standings, applies the −15% reset |
| Nothing during a normal run | Retries transient errors, falls back, records status |

## How it works without any frontend (right now)
The pipeline already runs headless: the scheduler fires a job per match window,
`orchestrator/pipeline.process_match` builds the card, **delivers it** to your
channels (file always; Telegram/console if configured), and records the outcome.
With no Telegram set up you still get the cards as files in `reports/`. Try it now:
```bash
DELIVERY_CHANNELS=file,console python -m orchestrator.run
```

## How you know a run succeeded, failed, fell back, or got stuck
The system is **loud on failure** — silence never means "unknown":
- Every match-window job writes a row to the **run ledger** (`runs` table):
  `ok` / `failed` (+ why) / `started` that never finished (= stuck), whether it
  **fell back** to a backup source, and whether the **card was delivered**.
- On any failure, an **alert is pushed** to your channels immediately:
  "⚠️ Pipeline FAILED — Norway vs France: odds source down".
- A **daily health summary** is pushed:
  "runs 6 | ok 5 | failed 1 | fallbacks 1 | cards 5" + the failure details.
- Check anytime:
  ```python
  from core.obs.runs import runs; print(runs().summary(24))
  ```
  or open the dashboard. See `docs/RELIABILITY.md` for the retry/fallback details.

## If you ever want more (optional, later)
A ~100-line **Streamlit** app over the same SQLite gives a live, clickable view
(upcoming matches, click a game to see the full analysis, standings, health). It
needs a running server, so only add it if you want a shared group screen. The data
is already there — it's purely a presentation choice.
