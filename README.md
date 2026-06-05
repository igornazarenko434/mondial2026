# Mondial 2026 — Prediction Agent System

Decision-support for the friends' Toto Mondial 2026 pool. For every World Cup
match it gathers stats + Elo + injuries + live odds, models the score, and
emits the **1X2 + exact-score pick that maximizes expected points** under the
group's rules (not the most-likely score) — delivered to your phone ~7 min before
kickoff. Plus a futures module (winner / top scorer / Cinderella / fighter) and a
scoring/standings engine.

> Advisor, not bettor: it tells you what to pick; you enter it in the Toto app.
> For fun only — bookmaker odds are a data signal + the group's scoring
> multiplier, no real-money wagering.

## Current state (what's built vs. to-wire)
**Built & unit-tested (132 tests passing):**
- ✅ **Scoring engine** (`core/scoring/`) — exact rules (France 2-1 → 3.000, draw 1-1 → 5.625).
- ✅ **EV optimizer** (`core/decision/`) — the edge; proven == brute-force expectation.
- ✅ **Models** (`core/models/`) — Dixon-Coles matrix, Elo, blend, de-vig (`core/data/oddsapi.py`).
- ✅ **LLM router** (`core/llm/`) — Claude→Gemini→OpenAI, configurable + fallback.
- ✅ **Observability** (`core/obs/`) — tracing, JSON logs, cost/quota ledger, rate limiter.
- ✅ **Reliability** (`core/reliability.py`) — retry/backoff + source fallback.
- ✅ **Delivery** (`core/delivery/`) — file + Telegram + console fan-out.
- ✅ **Pipeline** (`orchestrator/pipeline.py`) — per-match run: retry→deliver→record, loud on failure.
- ✅ **Scheduler + watchdog** (`schedule/`) — concurrent (threads), heartbeat, stuck-job detection.
- ✅ **Tools** (`tools/`) — static dashboard + metrics CLI.

**To wire with live data (see `CLAUDE.md` day-by-day):** real fixture ingest,
stats/Elo scrapers, model fit + backtest, live odds matching, results→standings,
futures Monte-Carlo, news agent.

## Quickstart
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # add free API keys; optional Telegram for phone push
pytest tests/ -q                # 132 tests should pass
python -m store.db              # create the SQLite store
python -m orchestrator.run      # full pipeline demo (builds + delivers a card, no keys needed)
python -m tools.dashboard       # writes reports/dashboard.html
python -m tools.metrics         # show metrics
```

## Free API keys (all free tiers)
- football-data.org — fixtures/results (WC in free tier)
- the-odds-api.com — odds (500 req/mo)
- api-football.com — lineups/injuries (100 req/day)
- LLM: Claude subscription (Agent SDK credit) or Gemini free tier — see `docs/LLM_AND_COSTS.md`

## Layout
```
config/        rules.py (scoring/payouts) · llm.py · observability.py · preflight.py
core/scoring/  rules engine (+ tests)            core/decision/  EV optimizer (the edge)
core/models/   dixon_coles · elo · blend · montecarlo(futures)
core/data/     football_data · oddsapi(devig) · api_football · soccerdata_io · teams(normalize)
core/llm/      model-agnostic router + providers core/obs/   tracing·logs·cost·ratelimit·runs
core/delivery/ file · telegram · console         core/reliability.py  retry + fallback
orchestrator/  run.py demo · pipeline.py · agents/news_agent.py
schedule/      scheduler(catch-up) · runner(daemon) · watchdog
store/         sqlite schema · db · repo(upcoming/finished)
tools/         dashboard.py · metrics.py
data/          wc2026 groups + detonators        docs/   design·user·cost·reliability·scheduling·failure-modes
tests/         pytest (40)
```

Docs: `docs/BLUEPRINT.md` (full design), `docs/USER_GUIDE.md`
(how you use it), `docs/RELIABILITY.md`, `docs/SCHEDULING.md`,
`docs/OBSERVABILITY.md`, `docs/COST_AND_LIMITS.md`, `docs/LLM_AND_COSTS.md`,
`docs/VERIFICATION.md`. Build order: `CLAUDE.md`.
