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
11. Run `pytest tests/ -q` after every change (263 tests should stay green).

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
| `core/data/api_football.py` (lineups/injuries/backup) | ✅ Day-8 | live-verify lineup payload + aliases once `/fixtures` populates (~24h pre-kickoff) |
| `core/data/web_search.py` (Brave Search, Day-8 news) | ✅ Day-8 | nothing; 3-layer budget brake keeps us at $0 |
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
- [x] **Day 5 — scoring at scale (DONE).** `core/scoring/standings_writer.py`
      with `update_standings()` (idempotent; -15% group reset on first KO
      match scored; preserves futures_points for Day 7), `score_one_match()`
      (returns None on missing data; catches unknown stage labels), and
      `compute_prize_distribution()` (§5 prize ladder).
      `core/scoring/penalties.py::predict_shootout(elo_h, elo_a)` — bounded
      ±0.05 from 50/50 per literature (tanh edge).
      LIVE-VERIFIED Jun 2026: Mexico 2-0 exact + detonator → 12.025 pts
      (matches PDF §12). R16 finish triggers -15% reset → 10.221 / 4.5 / 14.721.
      Penalty predictions: Spain v Brazil 52.0%, Argentina v Curacao 54.7%,
      equal Elo exact 50.0%.
      BUG FIXED in `latest_snapshot`: was using `ORDER BY captured_at DESC`
      on string labels (`"T-pre-tourney" > "T-7m"` in ASCII), picked stale
      pre-tournament snapshot over the T-7m lock. Now walks `WINDOW_PREFERENCE`
      in order. 3 regression tests pin the fix.
- [x] **Day 6 — card + delivery with full audit trail (DONE).**
      `core/decision/build_card.py` — central glue: loads four signals
      (`dixon_coles`/`elo`/`market`/`news`) each wrapped in try/except, stamps
      audit fields (`signals_used`, `signals_failed`, `failure_reasons`,
      `ev_pathway`), predicts `penalty_winner` on KO+draw, persists to
      `predictions` via upsert. NEVER RAISES (golden rule #10).
      `core/delivery/base.render_card` — compact ≤8 lines normal / ≤9 KO+pen;
      `Signals:` line with inline `⚠signal: reason`; modal-line collapse when
      modal==pick; strict cap enforced (log warning runtime, asserted in tests).
      `schedule/runner.py.__main__` now uses real `build_card` (was `demo_card`).
      `config/rules.DRAW_PEN_THRESHOLD = 0.15`.
      LIVE-VERIFIED Jun 2026 across 5 scenarios:
        - All 4 signals (Mexico v South Africa): pick = Draw 0-0 EV 3.42 →
          6.84 with detonator (EV ≠ modal — exactly the system's edge).
        - KO synthetic (R16): penalty_winner = Mexico (54%), render shows
          "► If pens: Mexico (54%)", 8 lines total.
        - Market-failed: ev_pathway=modal_fallback, render shows
          "Signals: DC+Elo+News  ⚠market: odds_api over budget or no event".
        - All 4 signals fail: render still works with all 4 ⚠ markers.
        - Real Telegram delivery: card landed on phone (✓).
      AUDITABILITY GOLDEN RULE pinned via parametrized test across 6 scenarios
      (`test_auditability_golden_rule[...]`) — every signal must appear in
      `signals_used` OR `signals_failed`; silent bypass is impossible by
      construction.
      CARRIED OVER TO DAY 9: `events_cache` parameter is BUILT into
      build_card but the SchedulerDaemon doesn't batch yet — each match
      currently re-fetches odds. Wire daemon to fetch ONE per window and
      pass `events_cache=` to every dispatched match in that window (cuts
      tournament-wide credits from ~300 → ~12).
      CARRIED OVER TO DAY 8: when news_agent.search_queries is wired to
      web-search/API-Football, build_card will start counting "news" as a
      meaningful contributor (currently analyze_safe returns NEUTRAL → news
      is in signals_used but contributes 0 delta).
- [x] **Day 7 — futures lock (DONE — pre-deadline).** Three EV-optimal picks
      computed via live data + Monte Carlo (fighter is intentionally manual).
      Deliverables:
      - `core/models/montecarlo.py` — 20k tournament-bracket simulator
        (Poisson goals from DC fit + Elo-edge penalty shootouts; snake-seeded
        R32 with intra-group rematch avoidance).
      - `core/data/futures_odds.py` — outright market fetchers (winner +
        topscorer with auto-detect; budget-guarded; canonical name normalize).
      - `tools/futures_lock.py` — orchestrator: load data → MC → market → EV
        tables → JSON + Telegram-able pretty-print. Uses **market-prior ×
        sqrt(MC team factor) hybrid** for scorer fallback when no live market.
      - `docs/FUTURES_LOCK_2026.md` — picks + reasoning + pool-win analysis.
      Live picks (cross-checked against ESPN/SI/FOX/RotoWire/Goal.com Jun 2026):
        - WINNER: **Portugal** (EV 3.04, +0.14 margin, market-driven)
        - CINDERELLA: **Uzbekistan** (EV 0.99, +0.62 margin, MC-driven)
        - SCORER: **Mbappé** (EV 3.39, +0.42 margin); contrarian alt **Bellingham** (2.88)
        - FIGHTER: manual per user
      Bug fix bundled: Qatar missing `cinderella_listed` flag in groups CSV.
      Tests: +26 (12 MC + 7 odds + 7 audit). User must enter picks in the
      Toto app before **11.06 21:59 Israel**.
- [x] **Day 8 — news agent (DONE — full wire + observability).**
      Live-verified Jun 2026: end-to-end build_card with real Gemini call
      returned `Signals: DC+Elo+Market+News(gemini)`; trace
      `4664ef0e…`/`7fc52e5d…` visible in Honeycomb with all child spans
      (gemini.complete, brave_search.web, api_football.fixtures/teams,
      football_data.wc_matches).
      Deliverables:
      - `core/data/web_search.py` — Brave Search adapter with 3-layer
        budget brake (key check → 90% monthly brake → 60/day cap).
      - `core/data/api_football.py` — real `find_fixture_id` / `fetch_lineups`
        / `fetch_injuries` / `find_team_id` (with global fallback).
      - `orchestrator/agents/news_agent.py` — 5-layer guardrails (L1 dated
        WC2026-anchored queries → L2 source-side freshness filter → L3 capped
        context with dated headers → L4 SYSTEM prompt with 2 worked examples
        → L5 strict→regex→NEUTRAL parse + ±0.6 clamp). `analyze()` returns
        provider/fallbacks_used; `analyze_safe()` adds failure on degraded
        path. `read_prior_deltas()` reuses T-60m's stored deltas at T-15m
        (saves ~70% of T-15m LLM+Brave calls when XI is confirmed).
      - `core/decision/build_card.py` — gather_context wired at T-24h/T-60m/
        T-15m; news_deltas flow to `match_card` → shift DC expected goals
        (`lh + news_deltas[0]`, `la + news_deltas[1]`). Card carries flat
        `news_provider` / `news_fallbacks_used` / `news_failure` fields
        (queryable from `predictions.payload_json`).
      - `core/delivery/base.py::render_card` — Signals line shows
        `News(gemini)` on success, `⚠news: <reason>` on failure.
      - `core/llm/router.py` — every LLM call wrapped in
        `obs.external_call(provider, "complete")`; `last_provider` +
        `last_fallbacks` stamped on success so callers know which model
        answered.
      - `tools/obs_audit.py` — end-to-end CLI that fires one live probe per
        provider, prints config matrix vs. each provider's published free-
        tier ceiling, verifies spans + ledger rows. Run with
        `OTEL_TRACES_EXPORTER=console` to inspect spans locally.
      Budget math (live, Jun 2026): Brave free credit = 1000 req/mo;
      104 matches × (3 T-24h + 4 T-60m + 2 T-15m worst case) = 936 reqs →
      fits in 1000 with 6% headroom. T-15m cache reuse drops it further.
      Current usage: 4/1000 ($0.02 cost, $0.00 OOP).
      Telegram bot + odds_api `/sports` (free) gaps closed in commit
      `07e9ee7` — every `requests.get/post` in non-test code is now
      inside `obs.external_call(...)`. PROVIDER_LIMITS + PRICING in
      `config/observability.py` cover all 10 providers.
      Tests: +28 over Day-7 baseline (313 total green): per-window query
      counts, gather_context assembly, JSON parse tiers, ±0.6 clamp,
      T-15m cache reuse, router provider stamping, render audit visibility.
      CARRIED OVER (cannot verify pre-tournament, by design): API-Football
      lineup payloads and team-name aliases will only be live-testable when
      `/fixtures` returns rows for WC2026 (`league=1&season=2026` was empty
      as of 2026-06-06; expected to populate ~24h before kickoff). Code
      degrades gracefully to Brave-only context if API-Football is empty.
- [ ] **Day 9 — orchestrate.** The daemon's fixture source + real
      `build_card` are ALREADY WIRED in `schedule/runner.py.__main__`
      (Day 6); the ThreadPoolExecutor + watchdog already work. Remaining
      Day-9 work:
      (a) **CARRIED FROM DAY 6: events_cache batching.** Before dispatching
          jobs in a tick where any `T-60m`/`T-15m`/`T-7m` window has matches
          due, the daemon should call `fetch_all_odds()` ONCE and pass the
          result as `events_cache=` to every match's `build_card`. Saves
          ~95% of odds_api credits. Suggested: thread a `WindowContext`
          object (with `events`, `now`, etc.) through `_run_job`.
      (b) Put the daemon under launchd (see docs/SCHEDULING.md).
      (c) Dry-run on day-1 fixtures; confirm `python -m tools.metrics`
          shows per-game data.
      (d) Optional: wrap workers as Claude Agent SDK subagents.
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
   - api-football: empty until tournament starts — re-audit ~24h pre-kickoff
     when `/fixtures?league=1&season=2026` first returns rows. Day-8 wired
     the client + graceful degradation; live payload-level alias check is
     the only remaining piece.
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
