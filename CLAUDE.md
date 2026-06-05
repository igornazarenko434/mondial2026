# Guidance for Claude Code — Mondial 2026

You are helping build a World Cup prediction system. Read this before working.

## Golden rules
1. **Never do points-affecting arithmetic in prose or the LLM.** All scoring and
   EV math lives in `core/scoring` and `core/decision` and is unit-tested. If you
   change scoring, update `tests/test_scoring.py` and keep the PDF examples green
   (France 2-1 → 3.000, draw 1-1 → 5.625, final 2-2 → 12.5).
2. **`config/rules.py` is the single source of truth** for tables/payouts. Don't
   hard-code rule numbers anywhere else.
3. **Keep agents stateless and idempotent** — read from the store, write back,
   safe to re-run. This is what makes parallel runs and retries safe.
4. **Respect free-tier quotas** — cache static data; only pull odds in the active
   match window (T-60m/-15m/-7m). Add a shared throttle before each external API.
5. **No RAG / vector DB** (data is live + structured). Don't add LangGraph yet —
   the flow is a scheduled pipeline. Add the Claude Agent SDK orchestrator last.
6. **Wrap every outbound API/LLM call in `obs.external_call(provider, endpoint)`**
   (see `core/obs/__init__.py`). It rate-limits (shared token bucket), traces,
   times, and records cost/quota automatically. Wrap each match-window job in
   `with obs.run(label):` for a correlation id. Never call an external service
   without going through the limiter — that's what keeps free tiers safe.
7. **LLM is model-agnostic** via `core/llm/router.py` (chain in `config/llm.py` /
   `LLM_PROVIDER_CHAIN`). Default to Claude (subscription credit) → Gemini (free).
   Never import a provider SDK directly in business logic.
8. **Run every match through `pipeline.process_match`** — it wraps build in
   retry+fallback, records run status, delivers the card, and **stays loud on
   failure** (alerts). Never deliver a card outside it; never let a job crash the
   scheduler. Outputs go through `core/delivery` only.
9. **No web frontend.** UX = push notifications + `reports/` files + the optional
   static `tools/dashboard.py`. Don't build a server UI unless explicitly asked.
10. **Build `build_card` to the graceful-degradation ladder** (docs/FAILURE_MODES.md):
    model+odds+news → model-only → Elo+market → neutral-news → (last resort) alert.
    It must NEVER raise; call `news_agent.analyze_safe`, check `ledger.over_budget`
    before odds pulls, normalize teams via `teams.normalize`, and devig defensively.
11. Run `pytest tests/ -q` after every change (171 tests should stay green).

## Current state — infrastructure already built (don't rebuild)
These layers exist, are tested, and run today with no API keys. Your job is to
feed them LIVE DATA, not to re-architect them:
- scoring engine, EV optimizer, Dixon-Coles/Elo/blend, de-vig (guarded)
- LLM router (`core/llm`), observability (`core/obs`: tracing/logs/cost/ratelimit/runs)
- reliability (`core/reliability`), delivery (`core/delivery`), pipeline
  (`orchestrator/pipeline`), scheduler+watchdog with catch-up & idempotency
  (`schedule/scheduler`,`runner`,`watchdog`), tools (`tools/dashboard`,`tools/metrics`)
- data: `football_data` (live, normalized, stage-mapped), `store/repo` (upcoming/
  finished), `teams` (name normalization), `config/preflight` (startup check)
- client stubs to fill: `oddsapi`, `api_football`, `soccerdata_io`, `montecarlo`
- win-strategy layer (`core/decision/strategy`): opt-in variance/position tilt on
  top of EV (default off). See docs/STRATEGY.md — max-EV ≠ max-P(win).
- side-bet recommender (`core/decision/sidebets`): daily over/under + yes/no from
  the per-match models.

## What the system produces — 3 outputs (all pure best-practice, NO standings logic by default)
1. **Per-game pick** — 1X2 + exact score that maximize expected points
   (`ev_optimizer.recommend`). This is the default and runs today.
2. **Overall / futures bets** — EV-ranked tables for winner / top scorer /
   Cinderella / fighter (`montecarlo` Day 7 + `config.rules` payouts); lock before
   11.06 21:59.
3. **Daily side bets** — over/under & yes/no recommended from the day's match
   models (`sidebets.recommend_total_goals` / `recommend_yes_no`).

## Position / standings strategy — OFF for now (enable later, mid-tournament)
The system does **NOT** consider your league position by default
(`STRATEGY_TILT=0` → pure expected-points per game). To turn it on later (e.g. if
you fall behind near the knockouts), set `STRATEGY_TILT=0.3–0.6` and pass a
standings context through `strategy.recommend_to_win(...)`. Until then, every pick
is the straight best-practice EV pick. Do not wire standings into `build_card` for
the MVP — keep it a clean, optional post-step.

## Component status matrix — exactly what exists vs what to implement
✅ built & tested (don't rebuild) · 🟡 stub to fill with live data · 🔌 wire-up only

| Component | Status | What YOU still do |
|---|---|---|
| `config/rules.py`, `config/llm.py`, `config/observability.py`, `config/strategy.py`, `config/preflight.py` | ✅ | nothing (tune values if desired) |
| `core/scoring/engine.py` (rules, EV helpers) | ✅ | nothing |
| `core/decision/ev_optimizer.py` (per-game pick) | ✅ | nothing |
| `core/decision/strategy.py` (win tilt) | ✅ **wired into pipeline, default-off** | enable later via `strategy_tilt` + context |
| `core/decision/sidebets.py` (daily over/under, yes/no) | ✅ | call it when the daily bet is published |
| `core/decision/futures.py` (pre-tournament EV ranker §7–10) | ✅ | feed it probabilities (market odds via `implied_probs`, or montecarlo) |
| `core/models/dixon_coles.py` `score_matrix` | ✅ | — ; `fit_strengths` 🟡 feed real results (Day 3) |
| `core/models/elo.py`, `blend.py` | ✅ | recalibrate `draw_base`/weights (Day 3) |
| `core/models/montecarlo.py` (futures prob source) | 🟡 stub | optional: bracket sim → probs (or just use market futures odds) (Day 7) |
| `core/data/football_data.py` (ingest + `refresh`/`tag_detonators`) | ✅ tested offline | add real API key (Day 1) |
| `core/data/cache.py` (daily disk cache) | ✅ | nothing |
| `core/data/soccerdata_io.py` (Elo/FBref loaders: cache+normalize+lookup) | ✅ logic; live `_fetch_eloratings`/`_read_fbref` 🟡 | wire the scrapes (Day 2) |
| `core/data/oddsapi.py` (devig, resolve_wc_key) | ✅ ; `fetch_match_odds` 🟡 | finish event→fixture matching (Day 4) |
| `core/data/api_football.py` (lineups/injuries/backup) | 🟡 stub | implement (Day 8 / fallback) |
| `core/data/teams.py` (normalize) | ✅ | extend aliases if a name slips through |
| `core/obs/*`, `core/reliability.py`, `core/delivery/*` | ✅ | set Telegram creds (optional) |
| `orchestrator/pipeline.py` (run+retry+deliver+strategy) | ✅ | nothing |
| **`build_card(match)` — the real model→card function** | ❌ **MISSING (only `demo_card`)** | **Day 6: implement to the degradation ladder; this is the central glue** |
| `schedule/scheduler.py`, `runner.py`, `watchdog.py` | ✅ wired to store+ingest | add API key, run under systemd (Day 9) |
| `store/db.py`, `store/repo.py` | ✅ | nothing |
| `tools/dashboard.py`, `tools/metrics.py` | ✅ | nothing |
| scoring → `standings` table wiring | ❌ to build | **Day 5** |

**Do FIRST / for now (MVP, no standings logic):** Day 1 calendar → Day 3 model →
Day 4 odds → **Day 6 `build_card`** (this turns the model into the per-game card)
→ deliver. That alone gives live per-game picks. Futures (Day 7) before 11.06 21:59.
Side bets work today via `sidebets`. Everything else is enhancement.

## Build order (each step is a good, self-contained task)
- [x] All infrastructure above (built + tested).
- [x] **Day 1 — calendar (code DONE, tested offline).** `football_data.refresh(conn)`
      = ingest + stage-map + name-normalize + **detonator tagging** (order-independent,
      survives re-ingest) + utcDate/tz guards. Daemon calls `refresh`. ON YOUR
      MACHINE: add `FOOTBALL_DATA_API_KEY`, run `store.db.init_db()` then
      `football_data.refresh(connect())`; verify ~104 matches + R32 stage code (extend
      `RULES_STAGE` if the live code differs) + `repo.upcoming_matches` returns today's.
- [x] **Day 2 — data agent (logic DONE, tested offline).** `soccerdata_io`
      Elo + FBref loaders with daily cache (`cache.py`), name normalization, and
      `elo_of`/`match_elos` lookup (proven to feed the model). ON YOUR MACHINE: wire
      `_fetch_eloratings` (eloratings.net/2026) and `_read_fbref` (soccerdata.FBref),
      or pass `fetch=`/`read=`; everything around them is built & tested.
- [x] **Day 3 — model (DONE incl. assembler + calibrate; needs live data).**
      `fit.py` (results→fit→expected_goals), `backtest.py` (log-loss/Brier/tune),
      `predict.py` (the model→decision ASSEMBLER: fit+elo+market+news → card,
      degradation-safe), `tools/calibrate.py` (load→fit→tune→recommend weights).
      ON YOUR MACHINE: wire `results_io._fetch_live`, run `tools.calibrate.run(...)`,
      paste the recommended weights into `config.rules.BLEND_WEIGHTS`.
- [x] **Day 4 — odds (DONE incl. live audit).** `oddsapi.fetch_match_odds`
      with budget-guarded fetch (`ledger.over_budget('odds_api')`), `fetch_all_odds`
      batch path (1 credit returns all events), `match_event_to_fixture` (canonical
      names + ±36h date window — prevents same-team friendlies false-matching a
      WC fixture), `pick_book` (Pinnacle → Betfair Exchange → Betfair) with
      `consensus_book` synthetic fallback, `snapshot_odds` upserting into
      `odds_snapshots`, `latest_snapshot` reading the sharpest available.
      LIVE-AUDIT JUN 2026: 1 credit fetched 72 events, 72/72 WC fixtures matched
      (100% incl. Bosnia after alias fix), 70 Pinnacle + 2 Betfair, Mexico v
      South Africa devig = 67/21/12 (textbook heavy-favorite), round-trip OK.
      Re-run the audit script if `the-odds-api` adds new team-name spellings.
- [ ] **Day 5 — scoring at scale.** Wire actual results → `score_match` →
      `standings` table; implement the -15% reset and prize split end-to-end.
      NEW: **penalty winner prediction for knockout draws** —
      `core/scoring/penalties.py::predict_shootout(elo_h, elo_a) -> {"winner":
      "H"|"A", "p_winner": float}`. Use small Elo edge (penalties are ~50/50
      ± weak skill differential — bound `|p_winner - 0.5| <= 0.05` per literature).
      Carry through to card on KO+draw branch. Add unit tests pinning the bound.
- [ ] **Day 6 — card + delivery (with FULL AUDIT TRAIL).** Persist `recommend()`
      to `predictions`, route through `orchestrator.pipeline.process_match`,
      deliver via `core/delivery`, record run-status. Wire `daily_summary()` and
      schedule `tools/dashboard.py`. Strategy tilt as opt-in post-step (default 0).
      NEW: **audit-trail fields on every card**, compact one-line format on the
      Telegram message:
        - `signals_used`:   list[str] — actually fed into the matrix
                            (e.g. ["dixon_coles","elo","market","news"])
        - `signals_failed`: list[str] — attempted but degraded out
        - `failure_reasons`: dict[str, str] — why each failed, ≤80 chars each
                            (e.g. {"market":"odds_api over budget",
                                   "news":"news_agent: gemini 429, claude empty"})
        - `ev_pathway`: "ev_optimized" | "modal_fallback"  — which decision branch
        - `penalty_winner` (KO only): {"winner": "H"|"A", "p_winner": float}
                                      — set ONLY if stage != Group AND model_prob
                                      gives draw >= 15%; None otherwise.
      TELEGRAM RENDER (compact, ≤8 lines including header):
        ⚽ <home> vs <away> — <kickoff> (<stage> <group>)  ⚡DETONATOR x2
        Locked odds: <home> <H> / Draw <D> / <away> <A>
        Model: <home> 22% / Draw 26% / <away> 52%
        ► Pick: France win, exact <home> 1 — <away> 2   (likeliest 0-1)
        ► If pens: France  (51%)             ← only on KO+draw branch
        Expected points ≈ 1.90  → ×2 detonator ≈ 3.80
        Signals: DC+Elo+Market+News   ← or with failures: "DC+Elo  ⚠market: over_budget"
        ℹ <up to 2 context bullets, ≤60 chars each>
      Update render_card to enforce ≤8 lines, truncate context >2 bullets, fail
      gracefully on missing fields. Add render tests pinning each line shape and
      the failure-message variant.
      AUDITABILITY GOLDEN RULE: every signal MUST be reflected either in
      signals_used OR signals_failed+failure_reasons — never both absent (silent
      bypass = bug). Add a regression test enforcing this on build_card output.
- [ ] **Day 7 — futures (pre-tournament bets).** The EV ranker is BUILT
      (`core/decision/futures.py`). Feed it probabilities — simplest/sharpest is
      **de-vigged market futures odds** via `futures.implied_probs(odds)` →
      `recommend_futures({"winner":..,"scorer":..,"cinderella":..,"fighter":..})`.
      Optionally build `montecarlo.py` for model-based probs instead. Lock the 4
      picks before 11.06 21:59.
- [ ] **Day 8 — news agent.** Rubric, query builder, window/budget config and
      clamping are DONE (news_agent.py, config/news.py, docs/NEWS_AGENT_PLAYBOOK.md).
      Wire search_queries() to web-search / API-Football lineup+injury tools, pass
      results as context_text, apply returned deltas to DC expected goals in
      build_card. Always call analyze_safe; only search when should_search(window).
- [ ] **Day 9 — orchestrate.** Wire `schedule/runner.py` fixtures to read upcoming
      matches from SQLite, and `build_card` to the real model pipeline. The daemon
      (ThreadPoolExecutor) already runs simultaneous kickoffs concurrently and runs
      the watchdog/heartbeat each tick. Put it under systemd/launchd (see
      docs/SCHEDULING.md). Optionally wrap the workers as Claude Agent SDK
      subagents. Dry-run on day-1 fixtures; confirm `python -m tools.metrics` shows
      per-game data.
- [ ] **Day 10 — harden.** **Full BLEND_WEIGHTS calibration** — two paths:
      (a) PRE-TOURNAMENT WARM-START (~50 credits, optional). the-odds-api
          DOES expose a historical-odds endpoint on the free tier (corrected
          Jun 2026 — earlier docs claimed otherwise). It costs **10× the
          standard rate** (`GET /historical/sports/<key>/odds`: 1 market ×
          1 region × 10 = 10 credits/call), so the 500/mo budget allows
          ~50 historical calls. Pull odds for the last ~50 played
          internationals + match to results we already have in martj42 →
          full 3-source samples → real tune NOW.
      (b) POST-TOURNAMENT (preferred, free). After ≥20 played WC matches
          with locked T-7m odds + actual scores, re-run
          `tools.calibrate.run(...)` with the full 3-source grid (don't
          hold market fixed) and paste the winning triple.
      Fallback historical archives if (a) is exhausted: football-data.co.uk
      (European leagues, club only), Kaggle `mexwell/historical-football-
      resultsbetting-odds-data`, github.com/iredchuk/soccer-bookmaker-odds,
      footballcsv.github.io (results only, no odds). Most are club-focused —
      international-team odds archives are sparse, so (a) is the cleanest
      pre-tournament option.
      Partial Step-B DC-vs-Elo tune on real data (Jun 2026) showed DC/Elo
      ratio 0.75/0.25 → rescales to (0.375, 0.125, 0.50) with market=0.50
      fixed; current (0.30, 0.20, 0.50) defaults are within 0.4% of optimum
      so no urgent change.
      Plus: retries, throttle, finalize futures. Confirm
      `ledger().quota_status(...)` under all free quotas; optionally start
      Jaeger for a full match-window trace (see `docs/OBSERVABILITY.md`).

## Cross-day audit checklist (run after EVERY day's wire-up)

1. **Edge-case team-name aliases per data source.** The 5 sites use 5 different
   spellings; `core/data/teams.py` must canonicalize them all. Known per source
   (regression-tested in `tests/test_data_wiring.py`):
   - football-data.org: "Korea Republic", "Cabo Verde", "Cape Verde Islands"
   - the-odds-api: "Bosnia & Herzegovina" (with `&`), "Czech Republic"
   - eloratings.net: 2-letter codes via `eloratings_codes.py`
   - martj42 CSV: "Korea Republic", "Cabo Verde", "Cote d'Ivoire"
   - api-football: empty until tournament starts — audit at Day 8
   After any new source addition, run the audit:
   `for n in {api_names}: assert normalize(n) in groups_truth`.
2. **Cost-ledger reconciliation.** `obs.cost.ledger().quota_status(provider)`
   for every provider after a window. Flag >50% used as warning, >80% as alert.
3. **Robustness pass on every external call.** Each `requests.get/post` must be
   inside `obs.external_call(...)` (rate limit + cost recorded). Run
   `grep -rn 'requests.\(get\|post\)' core/data/ orchestrator/` to verify zero
   ungated calls land in main.

> Observability is already wired (logging + cost ledger always on; OTel tracing
> optional). As you build each step, keep external calls inside
> `obs.external_call(...)` and jobs inside `obs.run(...)`. See
> `docs/COST_AND_LIMITS.md` for the full-scale budget and `docs/OBSERVABILITY.md`
> for how to trace live.

## Backlog — rule cases intentionally NOT yet automated (do after Day 10 if wanted)
These are manual in the spreadsheet today; port to `core/scoring` only if you want
full automation. None block the MVP.
- Penalty-shootout partial credit (§15c-e, §16c-d) — knockout games decided on pens.
- The "fighter" (§10) full ranking math — the futures pick is captured; ranking isn't.
- Extra-time→120' result mapping for knockout scoring.

## Stage mapping note
`football_data.RULES_STAGE` already maps football-data codes (GROUP_STAGE→Group,
LAST_32→R32, LAST_16→R16, QUARTER_FINALS→QF, SEMI_FINALS→SF, THIRD_PLACE→3rd,
FINAL→Final) and `ingest()` stores the rules stage directly, so `score_match`
gets the right stage. Verify the round-of-32 code string against the live API on
Day 1 (48-team format) and extend the map if needed.

## Testing the edge
`orchestrator/run.py` is the integration smoke test. Keep it runnable with no
keys (placeholder inputs) so you can always eyeball the pipeline.
