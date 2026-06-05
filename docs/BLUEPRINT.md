# Toto Mondial 2026 — Prediction Agent System
### Full implementation spec (free data sources · Claude Code build · live by 10 June 2026)

> Companion files: `Mondial_2026_Prediction_Template.xlsx` (scoring sheet), `data/wc2026_groups.csv`, `data/wc2026_detonator_fixtures.csv`.

---

## 0. TL;DR — how the whole thing works

A scheduled pipeline wakes up before every match, gathers stats + Elo + injuries/lineups + live bookmaker odds, runs a probabilistic score model, blends it with the market, and **5–10 minutes before kickoff emits one recommendation: the 1X2 pick and the exact score that maximize *expected points* under your friends' scoring rules** — not the most-likely score. A thin Claude orchestrator drives it and writes the human-readable card; deterministic Python does all the math. It runs many matches in parallel (two simultaneous kickoffs are handled by independent jobs). A separate one-off module locks your four futures bets (winner / top scorer / Cinderella / fighter) before 11.06 21:59 by ranking options on Expected Value.

The single biggest edge: **points = base × bookmaker-odds, and exact scores reward rare scorelines**, so the optimum is an EV calculation, not a guess at the likeliest result.

---

## 1. The game, decoded into requirements

**Futures / wide bets — locked once before 11.06 21:59**

| Bet | Pick | Scoring | System job |
|---|---|---|---|
| Tournament winner (§7) | 1 team | Fixed pts if correct (Spain 20 … USA 170) | Rank by P(title) × payout |
| Top scorer / מלך השערים (§8) | 1 player | Fixed pts (Mbappé 20 … Depay 73) | Rank by expected tournament goals × payout |
| Cinderella (§9) | 1 surprise team | Fixed pts (Congo 15 … Curaçao 75) | P(deep run) × payout |
| The fighter / הלוחם (§10) | 1 team | 10 pts if finishes highest + ranking pts | P(deep run) weighted by seed |

**Per-match bets — before every game:** 1X2 direction · exact score · daily side bets (§17) · detonator games ×2 (§18).

**Scoring math the engine must reproduce (verified against your PDF examples):**
- Wrong direction → **0**.
- Right direction, wrong score → **base × odds(outcome)**, base = 1.0 group / 1.5 R32–QF / 2.0 SF·3rd·Final.
- Right direction **and** exact score → **scoreTable(winnerGoals, loserGoals) × odds(outcome)**.
- Detonator → final per-game points **× 2**. After group stage → **all totals × 0.85** (§14).
- Prize ladder (§5): 23 / 15 / 12.5 / 10.5 / 9 / 8 / 7 / 6 / 5 / 4 %. Tie-break (§19): most exact-score hits.

The three exact-score tables are encoded in the spreadsheet and were cross-checked two ways: the diagonal structure (each row starts at its draw cell) and the worked examples (France 2-1 → 1.5×2.0 = 3.000; 1-1 → 2.25×2.5 = 5.625; final 2-2 = 5). **Not auto-handled (manual entry):** penalty-shootout partial credit (§15c–e, §16c–d), daily side bets (§17), the fighter ranking detail (§10).

---

## 2. How the system knows *when* each game is and *who* plays

This is the foundation — everything else triggers off the fixture calendar.

**The 12 groups are already collected** (`data/wc2026_groups.csv`, from the official FIFA final draw of 5 Dec 2025). Cross-check confirms your rules match reality: every detonator pairing is a real group game, and all your futures teams qualified.

**The authoritative live fixture feed** is the **football-data.org** API (World Cup competition is in the free tier). On every run the Data agent pulls:
```
GET /v4/competitions/WC/matches      -> id, utcDate, status, stage, group, homeTeam, awayTeam, score
```
Each match has a UTC kickoff (`utcDate`) and a `status` (SCHEDULED → TIMED → IN_PLAY → FINISHED). The system:
1. Stores every match row in SQLite (`matches` table) with kickoff converted to your local (Israel) time.
2. The scheduler reads upcoming `TIMED/SCHEDULED` matches and, for each, registers jobs at **T−24h, T−60m, T−15m, T−7m** before its kickoff.
3. Re-pulls the fixture list daily so knockout-bracket teams (currently TBD) and any time changes are picked up automatically. API-Football is the cross-check / backup feed and the source for confirmed lineups & injuries.

So you never hard-code dates: the calendar is data, refreshed daily, and the clock drives the agents. The 6 known detonator fixtures + opening matches are pre-seeded in `data/wc2026_detonator_fixtures.csv`; the 4 knockout detonators are filled in once the bracket is set.

---

## 3. Architecture — orchestrator + workers, built to run in parallel

```
                         ┌────────────────────────────┐
                         │      ORCHESTRATOR AGENT      │  Claude (thin)
                         │  reads calendar, spawns one  │
                         │  job PER MATCH, makes call    │
                         └──────────────┬───────────────┘
        ┌──────────────┬────────────────┼────────────────┬──────────────┐
        ▼              ▼                ▼                 ▼              ▼
  ┌───────────┐ ┌────────────┐ ┌──────────────┐ ┌──────────────┐ ┌────────────┐
  │ DATA AGENT│ │ ODDS AGENT │ │ NEWS/INJURY  │ │ MODEL AGENT  │ │ SCORING/   │
  │ (Python)  │ │ (Python)   │ │ AGENT (LLM)  │ │ (Python)     │ │ STANDINGS  │
  └─────┬─────┘ └─────┬──────┘ └──────┬───────┘ └──────┬───────┘ └─────┬──────┘
        └─────────────┴───────────────┴────────────────┴───────────────┘
                                   ▼
                ┌────────────────────────────────────────┐
                │  SQLite (truth) + Parquet cache          │
                │  matches · odds_snapshots · stats ·       │
                │  predictions · results · standings        │
                └────────────────────────────────────────┘
```

**Parallelism & scale (handles two simultaneous kickoffs):**
- The unit of work is a **(match, time-window) job**. The scheduler is a job queue, not a single loop. Two 22:00 kickoffs = two independent job chains running concurrently.
- Implemented with a **`ThreadPoolExecutor`** (`schedule/runner.py`) so each match's data fetch, model run, and card generation proceed independently. The work is I/O-bound (API/web calls), so threads overlap perfectly and are lighter than processes — multiprocessing would be the wrong tool here (see §17 / `docs/SCHEDULING.md`).
- Every agent is **stateless and idempotent**: it reads inputs from the store, writes outputs back, and can be re-run safely. That's what makes parallel + retry safe.
- **Shared rate-limit budget:** a central token-bucket throttler in front of each external API so 5 parallel jobs don't blow the free quota. Odds pulls are scheduled only in the active window; static data is cached and never re-fetched.
- The match model itself is per-match independent, so a 12-match day is embarrassingly parallel.

---

## 4. Data sources — what, where (free), and the best "sites that count"

| Layer | Source (free) | Used for | Refresh |
|---|---|---|---|
| **Fixtures/results** | football-data.org (WC free tier) | calendar, scores, status | daily + per-window |
| **Backup fixtures + lineups + injuries** | API-Football (100 req/day free) | confirmed XI, injuries, suspensions | T−60m, T−15m |
| **Team & player stats, xG** | `soccerdata` → FBref, Understat | attack/defence strength, form, xG | daily |
| **National-team Elo** | eloratings.net | strength prior, expected GD | daily |
| **Odds — the sharp benchmark** | **Pinnacle** & **Betfair Exchange** (via The Odds API free tier, 500 req/mo) | the scoring multiplier + market probabilities | T−60m, T−15m, **T−7m lock** |
| **Odds — aggregated consensus** | The Odds API (many books) / OddsPortal | consensus de-vig | per-window |
| **Squads / market value** | Transfermarkt, TheSportsDB | squad lists, player value, injury notes | daily |
| **News / lineups / context** | ESPN, BBC Sport, FIFA official, RotoWire/Sofascore predicted XI | motivation, rotation, weather | T−60m (web search) |

**Source audit (verified June 2026 — see `mondial2026/docs/SOURCES.md`):** all
sources are live, free, and reliable. Two notes baked into the code: the World Cup
**odds sport key is resolved dynamically** from The Odds API `/sports` (not
hard-coded), and football-data.org's free WC feed may lack **Round-of-32
placeholders** for the 48-team bracket — if so, API-Football becomes the primary
fixtures source via the existing fallback. Modeling is current: 2026 research
reaffirms **market odds are the best-calibrated signal** (our 0.50 market lean is
justified) with Dixon-Coles the interpretable baseline; an xG/Skellam signal is an
optional future add.

**Why Pinnacle / Betfair are "the sites that count":** they are the *sharpest* markets — ultra-low margin (~2–3%), they take sharp money and correct mispriced lines fast, so their **de-vigged prices are the best free probability estimate available** and are the standard benchmark for calibrating models. We treat their consensus as the anchor the model must beat or agree with. (This is purely a data signal — no wagering.)

**Confirmed lineups** are released by FIFA/teams **~1 hour before kickoff**, which is exactly why the T−60m window exists: that's when the News/Injury agent upgrades "predicted XI" to "confirmed XI" and the model re-runs.

---

## 5. Retrieval timing — what is pulled, and how long before kickoff

| Window | Agent(s) | Pulls / does | Why then |
|---|---|---|---|
| **Daily 06:00** | Data | full fixture list, stats, xG, Elo; refit model strengths | keep base data & calendar current |
| **T−24h** | Data, Model | snapshot team form; first projection | early lean, sanity check |
| **T−60m** | News/Injury, Odds | probable→confirmed XI, injuries, suspensions, weather, motivation; first odds pull | lineups land ~60m out |
| **T−15m** | Odds, Model | re-pull odds; re-run model with confirmed XI adjustments | near-final picture |
| **T−7m (LOCK)** | Odds, Model, Orchestrator | snapshot the **locked odds** (your scoring multiplier), recompute EV-optimal pick, **emit the final card** | your bets close just before kickoff (§3, §6) |
| **Post-match** | Data, Scoring | final score; compute points, update standings | scoring |

The "final call" deliberately lands inside your 5–10-minute pre-kickoff betting window.

---

## 6. The prediction & decision engine — exactly how it suggests a score

Three independent probability estimates → blend → EV optimization. All deterministic Python.

**Step 1 — three models of the match.**
1. **Dixon-Coles Poisson (time-weighted).** Fit attack/defence strength per team on historical international results, recent games weighted more. Outputs a full **score-line probability matrix** `P(home i goals, away j goals)` with the low-score (0-0/1-0/0-1) correction. This matrix is exactly what exact-score scoring needs.
2. **Elo expectation.** National-team Elo → win/draw/loss probabilities and expected goal difference. Used as a prior because international data is sparse.
3. **Market consensus.** De-vigged Pinnacle/Betfair + multi-book odds → market P(home/draw/away). The hardest-to-beat estimate; it anchors the blend.

**Step 2 — apply context adjustments.** The News agent returns structured deltas (e.g. "Norway already qualified, likely rests starters → −0.30 to Norway expected goals"; "Mbappé confirmed starting → +0.15 France"). These nudge the Dixon-Coles goal expectations before the matrix is recomputed.

**Step 3 — blend** into one score matrix:
`P_final = w1·DixonColes + w2·Elo + w3·Market` (default 0.30 / 0.20 / 0.50, market-leaning, tunable by backtest).

**Step 4 — the decision (the actual edge).** For *every* candidate scoreline (0-0 up to ~6-6) compute the **expected points under your rules**:
```
EV(score) = P_final(score) × scoreTableMultiplier(score, stage) × odds(direction_of_score) × detonator_factor
EV(direction only) = P_final(direction) × base(stage) × odds(direction) × detonator_factor
```
Then recommend:
- **Direction pick** = the 1X2 outcome with the highest summed EV.
- **Exact-score pick** = the single scoreline with the highest EV(score).
These can differ from each other and from the modal (most-likely) score — that is intended and is where points are won. The engine also reports the modal score and the model's confidence for transparency.

**Worked logic example (Norway vs France, detonator ×2, odds NOR 4.20 / draw 3.60 / FRA 1.85):** even though France 1-0 may be the *most likely* score, a longer-odds correct call like France 2-1 or a draw can carry higher EV once you multiply by odds and the rare-score table — the engine picks whichever maximizes EV, then doubles it for the detonator.

**Backtest/calibration before trusting it:** fit on results up to a cutoff, predict the last N internationals, check Brier/log-loss vs the market, and tune the blend weights. Ship only if it's calibrated (predicted 60% events happen ~60% of the time).

---

## 7. Futures module (run once, before 11.06 21:59)

Monte-Carlo the whole tournament from the match model: simulate all 104 matches (group → third-place-qualification logic → knockout bracket) thousands of times. From the simulations:
- **P(team wins title)** → winner EV table (× §7 payouts).
- **P(team reaches R16/QF/SF+)** weighted by seed → Cinderella & fighter EV tables (§9, §10).
- **Expected goals per player** (team goals × player share from FBref/xG) → top-scorer EV table (§8).

Pick the max-EV option in each market. The spreadsheet's Futures tab lets you sanity-check by hand.

---

## 8. Modern, correct techniques used (and what we deliberately skip)

- **Orchestrator–worker multi-agent pattern** (current best practice for this shape of problem): one planner, specialized workers with isolated context, results synthesized centrally. Built on the **Claude Agent SDK** subagents.
- **Tools over reasoning for anything numeric:** the LLM calls deterministic Python tools; it never does arithmetic that affects points. Reproducible + auditable.
- **Structured outputs / JSON tool schemas** for every agent boundary, so the News agent's "confirmed XI + deltas" is machine-readable, not prose.
- **Idempotent, stateless jobs + a job queue** for parallelism, retries, and scale.
- **Evaluation/guardrails:** model backtesting + a unit-tested scoring engine (asserts against your rules' worked examples) so a bad data pull can't silently corrupt a recommendation.
- **Caching + central rate-limiting** to live within free quotas.
- **Skip RAG / vector DBs** — your data is live and structured (APIs), not a static document corpus; retrieval = API calls + targeted web search, not embeddings.
- **Skip LangGraph to start** — the flow is mostly a deterministic scheduled pipeline; start framework-light (plain Python + Claude calls), add the Agent SDK orchestrator once the core works. Reach for LangGraph only if you later need explicit state-graph control or model-swapping.

---

## 9. The pre-game output — exact spec

Emitted at T−7m and written to `predictions` + a Markdown/Slack card.
**Every card carries full auditability** — which signals fed the model, which
were attempted-but-degraded-out (with one-line reasons), and for knockout
matches with non-trivial draw probability, the penalty-shootout pick. JSON shape:
```json
{
  "match_id": 401,
  "kickoff_local": "2026-06-26 22:00",
  "home": "Norway", "away": "France", "group": "I",
  "stage": "Group", "detonator": true,
  "locked_odds": {"H": 4.20, "D": 3.60, "A": 1.85},
  "model_prob": {"H": 0.22, "D": 0.26, "A": 0.52},
  "pick_direction": "A",
  "pick_exact_score": {"home": 1, "away": 2},
  "modal_score": {"home": 0, "away": 1},
  "expected_points": {"direction": 0.96, "exact": 1.90, "with_detonator": 3.80},
  "context": ["Norway likely rotates (qualification scenario)", "Mbappé confirmed starts"],
  "confidence": "medium",
  "data_freshness": {"odds": "T-7m", "lineups": "confirmed", "model_run": "T-7m"},

  /* === AUDIT TRAIL — set on every card (Day 6) ============================ */
  "signals_used":    ["dixon_coles", "elo", "market", "news"],
  "signals_failed":  [],
  "failure_reasons": {},      /* e.g. {"market": "odds_api over budget"} */
  "ev_pathway":      "ev_optimized",   /* or "modal_fallback" if no usable odds */

  /* === Penalties — set ONLY on knockouts where draw probability >= 15% ==== */
  "penalty_winner":  null     /* or {"winner": "H"|"A", "p_winner": 0.51} */
}
```
Human card — **compact, ≤8 lines including header** (efficient and straight to
the point; truncates context to 2 bullets). The `Signals:` line is the audit
trail; failures appear inline as `⚠<signal>: <one-line reason>` so the user
sees exactly what fed the pick and what didn't, without bloat:
```
⚽ Norway vs France — 26.06 22:00 (Group I)  ⚡DETONATOR x2
Locked odds: Norway 4.20 / Draw 3.60 / France 1.85
Model: Norway 22% / Draw 26% / France 52%
► Pick: France win    Exact: Norway 1 — France 2  (likeliest 0-1)
Expected points ≈ 1.90  → ×2 detonator ≈ 3.80
Signals: DC+Elo+Market+News
ℹ Norway may rest starters    ℹ Mbappé confirmed starts
```

Knockout draw branch adds **one extra line** (the penalty winner pick), so
the card is at most 9 lines on KO+draw:
```
► Pick: Draw           Exact: France 1 — Argentina 1   (likeliest 1-1)
► If pens: France  (51%)
```

Degradation example — when news fails and odds are over budget, the card
stays the same shape but the `Signals:` line tells you which paths failed
and why (so silent bypass is impossible):
```
Signals: DC+Elo   ⚠market: odds_api over budget   ⚠news: gemini 429 → claude empty
Expected points ≈ 1.30 ⚠ degraded (no market multiplier)
```

---

## 10. Scoring & standings engine

Pure-Python mirror of the spreadsheet: group/knockout/final tables, base points by stage, detonator ×2, −15% group reset, futures payouts, daily side bets, standings, prize split, exact-score tie-break. Unit-tested against the rules' worked examples. Feed it your picks + actual results → live points and projected standings, plus "what-if tonight" simulation.

---

## 11. Tech stack & repo layout

```
mondial2026/                # the actual repo (built & tested)
├── config/                # rules.py · llm.py · observability.py
├── core/
│   ├── scoring/           # rules engine (mirrors PDF) + tests        [built]
│   ├── decision/          # ev_optimizer.py  (picks max-EV score)     [built]
│   ├── models/            # dixon_coles · elo · blend · montecarlo    [built/stub]
│   ├── data/              # football_data · oddsapi · api_football · soccerdata_io
│   ├── llm/               # model-agnostic router + providers         [built]
│   ├── obs/               # tracing · logging · metrics · cost · ratelimit · runs [built]
│   ├── delivery/          # file · telegram · console                 [built]
│   └── reliability.py     # retry/backoff + fallback                  [built]
├── orchestrator/          # run.py demo · pipeline.py · agents/news_agent.py
├── schedule/              # scheduler · runner(daemon) · watchdog      [built]
├── store/                 # sqlite schema + db
├── tools/                 # dashboard.py · metrics.py                  [built]
├── data/                  # wc2026 groups + detonators (seeded)
├── docs/                  # design, user guide, cost, reliability, scheduling, obs
└── tests/                 # pytest (171 passing)
```
Python 3.11+, threads (`concurrent.futures`), `soccerdata`, `penaltyblog`/`scipy`,
`pandas`, `numpy`, `requests`, `python-dotenv`; optional `opentelemetry-*`,
`anthropic`/`google-genai`. Claude Agent SDK added last (Day 9). No paid services.

---

## 12. 10-day build plan (today 1 June → live 10 June)  ★ = MVP

> **The infrastructure is already built and tested** (scoring, EV optimizer,
> models, LLM router, observability, reliability, delivery, pipeline, scheduler,
> watchdog, tools). The remaining work is **wiring live data into it.** The
> authoritative, always-current day-by-day checklist lives in
> `mondial2026/CLAUDE.md`; the summary below mirrors it.

| Day | Work (wire live data into built infra) | ★ |
|---|---|---|
| Mon 1 | Keys; `store.db.init_db`; `football_data.ingest` → 104 matches in SQLite (stage mapped, detonators tagged) | ★ |
| Tue 2 | Data agent: stats/xG (`soccerdata_io`), Elo (eloratings) | ★ |
| Wed 3 | Fit Dixon-Coles on int'l results → expected goals → matrix; backtest; tune blend weights | ★ |
| Thu 4 | `oddsapi.fetch_match_odds`: match events, prefer Pinnacle/Betfair, snapshot lock, blend | ★ |
| Fri 5 | Wire results → `score_match` → `standings` (−15% reset, prize split) | ★ |
| Sat 6 | Persist `recommend()` to `predictions`; run via `pipeline.process_match`; delivery + daily summary + dashboard | ★ |
| Sun 7 | `montecarlo.py` futures sim → EV tables → lock 4 futures picks | ★ (before 11 Jun) |
| Mon 8 | News/Injury agent: `api_football` lineups/injuries + web search → structured deltas | |
| Tue 9 | Wire `schedule/runner` fixtures + real `build_card`; supervisor; parallel dry-run; check metrics | ★ |
| Wed 10 | Calibration, confirm quota headroom, optional Jaeger trace, finalize futures (lock before 11.06 21:59) | ★ |

**Irreducible MVP if time is short:** fixtures + odds de-vig + Elo/market blend +
(already-built) scoring engine + EV optimizer + futures EV tables + file delivery.
Dixon-Coles refinement, news agent, and full automation can land during the group
stage (it runs to 27 June).

---

## 13. Risks & honest caveats

- **You won't out-predict Pinnacle.** The edge is the **EV optimization under this specific scoring**, plus disciplined futures EV — not raw forecasting accuracy.
- **International data is sparse** → lean on Elo + market; the goal model is a refinement, not the anchor.
- **Free-tier quotas** → schedule odds pulls only near kickoff; cache static data; central throttle.
- **Scrapers break** (`soccerdata` depends on site HTML) → football-data.org is the reliable spine; scraped stats are enrichments.
- **Knockout TBD** → bracket detonators and opponents fill in automatically once the daily fixture pull sees them.
- **Penalty/side-bet/fighter edge cases** are manual in the spreadsheet; port them to the engine only if you want full automation.
- **For fun only** — odds are a data signal and your group's scoring multiplier; no real-money wagering.

---

## 14. Observability, cost & rate-limits (built in)

The system is instrumented end-to-end so you can trace it while it runs and never
silently burn a free quota. Implemented in the repo under `core/obs/` (see
`docs/OBSERVABILITY.md` and `docs/COST_AND_LIMITS.md`), modular and config-driven.

**Four pillars, all best-practice / current tech:**
- **Tracing — OpenTelemetry** (vendor-neutral standard). One trace per
  match-window job, with spans for data → odds → news → model → scoring. Exporter
  is configurable: console by default, or OTLP to a free **Jaeger/Grafana
  Tempo/Honeycomb** backend to watch live.
- **Structured JSON logging** with a `correlation_id` + `trace_id` on every line,
  so all logs for one job (e.g. `match-401-T7m`) are linkable.
- **Metrics** — counters (api calls, LLM tokens, errors) and latency histograms.
- **Cost/quota ledger** (SQLite, always-on, free) — records every external call
  with units/tokens/estimated cost, tracks usage per monthly/daily budget, and
  **warns at 80%**.

**One unified guard** wraps every outbound call:
`with obs.external_call("odds_api", "h2h"): ...` → acquires the shared
**token-bucket rate limit** (parallel-safe across simultaneous matches), opens a
span, times it, and records cost. Already wired into the odds/fixtures clients
and the LLM router. The whole layer degrades to safe no-ops if OTel isn't
installed — instrumentation never breaks the pipeline.

**Full-scale cost at a glance (104 matches):** expected out-of-pocket **$0** —
football-data/soccerdata/Elo free; The Odds API stays under its 500/mo credit
(batched, near-kickoff only — the one constraint, watched by the ledger);
API-Football ≪ 100/day; LLM covered by your Claude subscription credit with the
Gemini free tier as fallback. The token-bucket limiter handles bursty
simultaneous kickoffs. Full table in `docs/COST_AND_LIMITS.md`.

## 15. What you see & how you use it (no web frontend needed)

This system is an **advisor**: it does not bet in the friends' Toto app — it tells
you what to pick, and you enter it yourself. For a single user, a web frontend is
unnecessary effort. The best-practice, minimum-effort interface is **push
notifications + generated files** (see `docs/USER_GUIDE.md`):

- **Per-game card → your phone** ~7 min before kickoff via **Telegram** (free,
  ~20-min one-time setup): the pick, exact score, odds, model %, expected points,
  and key context. You read it and enter it in the Toto app.
- **Browsable record → files** in `reports/` (`feed.md` + per-match `.md`).
- **Optional one-screen view** → `tools/dashboard.py` renders a static
  `reports/dashboard.html` (upcoming picks, standings, run health, quota) — no
  server. A ~100-line Streamlit app is the only "real UI" worth adding, and only
  if the whole group wants a shared screen.

Delivery is a configurable fan-out (`core/delivery`, `DELIVERY_CHANNELS`): file
always on, Telegram/console optional; if one channel fails the others still get
the card.

## 16. Reliability — retry, fallback, and never failing silently

Implemented in `core/reliability.py` + `core/obs/runs.py` (see
`docs/RELIABILITY.md`). Every match-window job runs through
`orchestrator/pipeline.process_match`, which:
- **retries transient errors** (network/timeout/429/5xx) with exponential backoff
  + jitter, and **fails fast on permanent ones**;
- **falls back** across data sources (football-data → API-Football) and LLMs
  (Claude → Gemini → OpenAI);
- writes a **run-status row** (`ok` / `failed` + reason / `started`-but-stuck,
  which source served it, whether the card was delivered);
- **delivers** the card, and on any failure **pushes an alert** — the exception
  never crashes the scheduler or the next match's job;
- a **daily health summary** is pushed ("runs 6 | ok 5 | failed 1 | fallbacks 1
  | cards 5"), so missing summaries = a dead scheduler (your heartbeat).

You always know the system's state via `runs().summary(24)`, the pushed alerts,
or the dashboard — silence never means "unknown".

## 17. Scheduling, watchdog & concurrency

`schedule/runner.py` is the always-on **daemon**: every 60s it finds jobs due in
this window (T-24h/-60m/-15m/-7m) and dispatches them on a **ThreadPoolExecutor**,
so **two matches kicking off at the same time run concurrently** (verified by
tests). Threads — not multiprocessing — because the work is I/O-bound (API/odds/LLM
calls); the model math is microseconds. The shared, thread-safe rate limiter keeps
concurrent jobs within free-tier limits, and the ledgers are thread-safe.

**Watchdog (two layers, best practice):** run the daemon under **systemd/launchd**
so the OS restarts it if it dies (process liveness); the app's
`schedule/watchdog.py` writes a **heartbeat** each tick and alerts on a stale
heartbeat (scheduler died) or **stuck jobs** (started-but-never-finished). The
daily summary doubles as your heartbeat. Full detail in `docs/SCHEDULING.md`.

**Watching metrics:** the SQLite ledgers persist every call/run with a
correlation id, latency, tokens and cost — so `python -m tools.metrics
match-401-T-7m` shows per-game metrics and `tools/dashboard.py` shows them on one
page, with **no Grafana/Prometheus needed** (optional later via the OTel exporter).
See `docs/OBSERVABILITY.md`.

## 18. Failure modes & production hardening

Built to be production-ready without over-engineering — full table in
`docs/FAILURE_MODES.md`. The guarantees: **never silently miss a card, never
crash the loop, never send a wrong/duplicate card, degrade gracefully.** Key
mechanisms:
- **Catch-up scheduling** — a window missed during a restart still fires (up to a
  grace cap) before kickoff (`scheduler.due_jobs`).
- **Persistent idempotency** — the runs ledger (`runs.was_handled`) means a
  restart never re-sends a card.
- **Team-name normalization** (`core/data/teams.py`) — fixes the real cross-source
  mismatches ("Korea Republic"→"South Korea", "Cabo Verde"→"Cape Verde", …).
- **Graceful-degradation ladder** — model+odds+news → model-only → Elo+market →
  neutral-news → loud alert. `news_agent.analyze_safe` and the guarded `devig`
  ensure a missing LLM or bad odds never blocks a pick.
- **Budget pre-check** (`cost.over_budget`) — skip an odds pull that would exceed
  the free monthly credit instead of hitting a hard 429.
- **Preflight** (`config/preflight.py`) — reports enabled/degraded features at
  startup so misconfiguration is loud, not discovered at T-7m.
- **Watchdog + supervisor** — heartbeat + stuck-job detection; run under
  systemd/launchd for auto-restart.

## 19. Winning strategy — max-EV is the foundation, not the whole game

Honest audit (see `mondial2026/docs/STRATEGY.md`): the EV optimizer maximizes your
**expected total points**, but winning a top-heavy prize pool means maximizing
**P(finishing 1st)** — a different objective. Bracket-pool and DFS-tournament
theory both confirm: pure favourites/EV gets a *min-cash*, not first; to win you
must **differentiate and tune variance to your standing**.

So we add an **opt-in strategy layer** (`core/decision/strategy.py`) on top of EV:
- **Behind, time short** → take more variance (longer-odds/rarer score) among the
  near-EV-optimal picks.
- **Ahead** → protect: prefer the safer pick, hedge toward the field.
- **Default (`STRATEGY_TILT=0`)** → pure EV, unchanged.

It only ever chooses among the top-EV candidates (never reckless) and needs only
standings (no opponent-pick data). **Biggest leverage:** the longshot-weighted
**futures** (USA 170, Curaçao 75) are the highest-variance single decisions — a
slightly contrarian futures pick is the classic pool-winning differentiator; the
**−15% reset** is a built-in comeback point; **detonators** amplify variance.
Limit: football is noisy and the pool is small, so we keep the tilt *moderate* —
the engine allocates variance given good probabilities, it can't manufacture an
edge from luck.

---

### Appendix — data already collected
- `data/wc2026_groups.csv` — all 12 groups, 48 teams (FIFA final draw, 5 Dec 2025); Cinderella-eligible teams flagged.
- `data/wc2026_detonator_fixtures.csv` — the 6 known group-stage detonators (opening Mexico–South Africa 11.06 22:00 … Norway–France 26.06 22:00) + 4 knockout detonator placeholders.
- Full 104-match calendar with exact kickoff times is pulled live from football-data.org on Day 1 (not hard-coded).
