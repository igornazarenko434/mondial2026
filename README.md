# Mondial 2026 — Prediction Agent

[![tests](https://github.com/igornazarenko434/mondial2026/actions/workflows/test.yml/badge.svg)](https://github.com/igornazarenko434/mondial2026/actions/workflows/test.yml)
[![python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)

Decision-support agent for the friends' **Toto Mondial 2026** pool. For every
World Cup match it gathers stats + Elo + injuries + live odds, models the
score, and emits the **1X2 + exact-score pick that maximizes expected points**
under the group's scoring rules — delivered to your phone ~7 min before
kickoff. Plus a futures module (winner / top scorer / Cinderella / fighter)
and a scoring/standings engine.

> Advisor, not bettor: it tells you what to pick; you enter it in the Toto
> app. For fun only — bookmaker odds are a data signal + the group's scoring
> multiplier, no real-money wagering.

---

## Why this exists (the edge)

The pool's scoring is non-trivial: `points = base × bookmaker_odds`, exact
scores get a rare-scoreline multiplier, detonator matches double, group stage
points get a −15% reset, and futures pay fixed payouts ranked by upset risk
(USA 170, Curaçao 75, etc.). Under those rules **the optimal score to predict
is often not the most-likely score** — it's whichever scoreline maximizes
`P(score) × table_multiplier × odds`. That's a closed-form EV calculation, not
intuition. This system runs it deterministically for every match.

Verified against the rules PDF examples to **0.001 of brute force**: France 2-1
→ 3.000, draw 1-1 → 5.625, final 2-2 → 12.5. See [`docs/VERIFICATION.md`](docs/VERIFICATION.md).

---

## What it produces

Three best-practice outputs, all "pure" (no standings tilt by default):

1. **Per-game pick** — 1X2 + exact score that maximize expected points
   (`core/decision/ev_optimizer.py`).
2. **Futures / overall bets** — EV-ranked tables for winner / top scorer /
   Cinderella / fighter (`core/decision/futures.py`). Lock once before
   11.06.2026 21:59 Israel.
3. **Daily side bets** — over/under and yes/no recommended from the day's
   match models (`core/decision/sidebets.py`).

A win-strategy variance tilt (`core/decision/strategy.py`) is wired but
default-off; enable mid-tournament if you fall behind in the standings.

---

## How it works (90-second tour)

```
 calendar (football-data.org)  ──┐
 stats + xG (FBref/Understat)  ──┤
 Elo (eloratings.net)          ──┤── Dixon-Coles + Elo + de-vigged market ──┐
 confirmed lineups + injuries  ──┤    (Pinnacle/Betfair) → blended matrix    │
 (API-Football + news LLM)     ──┘                                            │
                                                                              ▼
                                          ┌──────────────────────────────────────────┐
                                          │  EV optimizer over every scoreline,      │
                                          │  given the LOCKED bookmaker odds at T-7m │
                                          │  → 1X2 + exact-score pick                 │
                                          └────────────────────┬─────────────────────┘
                                                               │
                              file ─ console ─ Telegram  ◀── delivery + run-status ledger
```

Scheduler dispatches one job chain per match at **T-24h / T-60m / T-15m /
T-7m (lock)** on a `ThreadPoolExecutor`, so simultaneous kickoffs run
concurrently. A shared token-bucket throttler keeps every external call inside
the free-tier quotas. Full architecture, data sources, and timing: [`docs/BLUEPRINT.md`](docs/BLUEPRINT.md).

---

## Status

Days 1–3 code-complete and tested (138 passing, 1 flaky concurrent-SQLite test
tracked); structural infrastructure is built. Remaining work is wiring live
data into it.

| Day | Task | Status |
|---|---|---|
| 1 | Calendar ingest (football-data.org → SQLite, stage map, detonator tag) | ✅ code, tested offline |
| 2 | Data agent (Elo + FBref loaders, daily cache, name normalize) | ✅ code, tested offline |
| 3 | Model (Dixon-Coles fit + backtest + calibrate + assembler) | ✅ code, tested offline |
| 4 | Odds (event→fixture match, Pinnacle/Betfair, snapshot lock) | ⏳ |
| 5 | Standings (results → score_match → standings, -15% reset, prize split) | ⏳ |
| 6 | `build_card` + delivery wiring (degradation ladder) | ⏳ |
| 7 | Futures lock (EV ranker built; feed market or MC probs) — **before 11.06 21:59** | ⏳ |
| 8 | News agent (web search + API-Football → structured deltas) | ⏳ |
| 9 | Orchestrate live (daemon under launchd; real `build_card`) | ⏳ |
| 10 | Harden (calibration, quota headroom, optional Jaeger trace) | ⏳ |

Day-by-day playbook and golden rules: [`CLAUDE.md`](CLAUDE.md).

---

## Quickstart

```bash
# clone
git clone https://github.com/igornazarenko434/mondial2026.git
cd mondial2026

# venv + deps
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# env (see Configuration below)
cp .env.example .env
# edit .env, paste your free-tier API keys

# tests (138 should pass; 1 may be flaky on concurrent SQLite)
pytest tests/ -q

# initialize the store
python -m store.db

# demo run (works with no live keys — placeholder card)
python -m orchestrator.run

# dashboard (writes reports/dashboard.html)
python -m tools.dashboard

# metrics CLI
python -m tools.metrics
```

---

## Configuration

All env vars and free-tier sources — copy `.env.example` to `.env` and fill in.

| Variable | Purpose | Source (free tier) |
|---|---|---|
| `FOOTBALL_DATA_API_KEY` | Fixtures / results / calendar | https://www.football-data.org |
| `ODDS_API_KEY` | Bookmaker odds (scoring multiplier) | https://the-odds-api.com (500 req/mo) |
| `API_FOOTBALL_KEY` | Confirmed lineups, injuries, backup fixtures | https://www.api-football.com (100 req/day) |
| `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` | LLM router (news agent + card writer) | Claude API or Google AI Studio (free) |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Phone-push delivery (optional) | @BotFather → token; @userinfobot → chat id |
| `DELIVERY_CHANNELS` | Comma-separated: `file,console,telegram` | default `file,console` |
| `LOCAL_TZ` | Local timezone for kickoff display | e.g. `Asia/Jerusalem` |
| `STRATEGY_TILT` | Optional 0..1 variance/position tilt | default `0` (pure EV) |
| `LLM_PROVIDER_CHAIN` | Router fallback chain | default `claude,gemini,openai` |

Full table including observability/scheduler tuning: see [`.env.example`](.env.example),
[`docs/COST_AND_LIMITS.md`](docs/COST_AND_LIMITS.md), [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md).

### Telegram (optional, ~3 min)

1. Create a bot with `@BotFather` → grab the token.
2. Open `@userinfobot` in Telegram → grab your chat id.
3. Paste both into `.env`, set `DELIVERY_CHANNELS=file,console,telegram`.
4. Smoke test:
   ```bash
   python -c "from dotenv import load_dotenv; load_dotenv('.env'); \
              from core import delivery; \
              delivery.alert('Mondial 2026', 'Telegram delivery wired')"
   ```

---

## Project layout

```
config/        rules · llm · observability · preflight · strategy · news
core/scoring/  rules engine (PDF-verified) + tests
core/decision/ ev_optimizer · futures · sidebets · strategy (win tilt)
core/models/   dixon_coles · elo · blend · fit · backtest · montecarlo · predict
core/data/     football_data · oddsapi(devig) · api_football · soccerdata_io ·
               cache · results_io · teams(normalize)
core/llm/      model-agnostic router + providers (Claude → Gemini → OpenAI)
core/obs/      tracing · logs · cost ledger · rate limit · runs ledger
core/delivery/ file · telegram · console fan-out
core/reliability.py    retry + source fallback
orchestrator/  run.py demo · pipeline.process_match · agents/news_agent
schedule/      scheduler (catch-up) · runner (daemon) · watchdog (heartbeat)
store/         SQLite schema · db · repo (upcoming/finished matches)
tools/         dashboard · metrics CLI · calibrate · seed_fixtures
data/          wc2026_groups.csv · wc2026_detonator_fixtures.csv
docs/          BLUEPRINT · USER_GUIDE · RELIABILITY · SCHEDULING · OBSERVABILITY ·
               COST_AND_LIMITS · LLM_AND_COSTS · STRATEGY · FAILURE_MODES ·
               SOURCES · VERIFICATION · NEWS_AGENT_PLAYBOOK · rules.pdf ·
               scoring_template.xlsx
tests/         pytest (138 tests)
```

---

## Documentation

| File | What it covers |
|---|---|
| [`docs/BLUEPRINT.md`](docs/BLUEPRINT.md) | Full system design (the canonical reference) |
| [`CLAUDE.md`](CLAUDE.md) | Golden rules + component-status matrix + day-by-day build plan |
| [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) | How to use the system day-to-day |
| [`docs/VERIFICATION.md`](docs/VERIFICATION.md) | Self-audit: rules → code → test |
| [`docs/RELIABILITY.md`](docs/RELIABILITY.md) | Retry / fallback / never-fail-silently |
| [`docs/SCHEDULING.md`](docs/SCHEDULING.md) | Scheduler + watchdog + concurrency |
| [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md) | Tracing / logging / metrics / cost ledger |
| [`docs/COST_AND_LIMITS.md`](docs/COST_AND_LIMITS.md) | Free-tier budgets, expected cost ($0) |
| [`docs/FAILURE_MODES.md`](docs/FAILURE_MODES.md) | Production-hardening playbook |
| [`docs/STRATEGY.md`](docs/STRATEGY.md) | Max-EV vs max-P(win): the strategy tilt |
| [`docs/SOURCES.md`](docs/SOURCES.md) | Data source audit |
| [`docs/LLM_AND_COSTS.md`](docs/LLM_AND_COSTS.md) | LLM router + provider costs |
| [`docs/NEWS_AGENT_PLAYBOOK.md`](docs/NEWS_AGENT_PLAYBOOK.md) | News/Injury agent rubric and budget |
| [`docs/rules.pdf`](docs/rules.pdf) | Original Toto Mondial 2026 rules (Hebrew) |
| [`docs/scoring_template.xlsx`](docs/scoring_template.xlsx) | Spreadsheet mirror of the scoring engine |

---

## Testing

```bash
pytest tests/ -q              # full suite (138 tests)
pytest tests/test_scoring.py  # rules engine alone
pytest tests/test_ev.py       # EV optimizer (proven == brute force ±0.001)
pytest tests/test_delivery.py # render_card + Telegram payload
```

Golden rule: **any change to scoring or EV math must keep the PDF examples
green** (France 2-1 → 3.000, draw 1-1 → 5.625, final 2-2 → 12.5).

CI runs the full suite on every push/PR to `main` — see
[`.github/workflows/test.yml`](.github/workflows/test.yml).

---

## Contributing / forking

This is a personal project tied to a specific friends' pool with specific
scoring rules, but the EV-under-custom-scoring pattern generalizes. If you
fork:

- The rules engine and EV optimizer (`core/scoring`, `core/decision`) are the
  reusable core.
- `config/rules.py` is the single source of truth for tables and payouts —
  swap in your own pool's rules there.
- The 10 `docs/*.md` files explain the design top-to-bottom; start with
  `BLUEPRINT.md`.

No license is currently declared — if you want to fork or reuse beyond
reading, please open an issue first.

---

## Disclaimer

For fun only. Bookmaker odds appear here as a **data signal** (the sharpest
free probability estimate) and because they're the scoring multiplier in the
friends' pool. The system does **not** place bets — it advises; you enter
picks in the Toto app yourself.

The model leans on the market because the market is hard to beat; the edge is
in optimizing expected points under custom scoring rules, not in
out-predicting Pinnacle.
