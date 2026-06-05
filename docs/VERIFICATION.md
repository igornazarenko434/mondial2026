# Self-audit — formulas, rules alignment, and data flows

This document is the honest cross-check: every scoring rule mapped to its code
and test, every formula justified, every agent mapped to the real data it
collects, and an explicit list of what is *not* yet automated.

## 1. Rules → code → test (all green)

| Rule (PDF §) | Where implemented | Verified by |
|---|---|---|
| 1X2 direction, group = 1.0 base (§12a) | `engine.score_match` `BASE_POINTS["group"]` | `test_group_direction_only` (=2.0 with odds 2.0) |
| Exact score × odds, group table (§12b) | `engine.exact_multiplier` + `SCORE_TABLE["group"]` | `test_group_exact_2_1` → **3.000** |
| Draw 1-1 exact (§12c) | same | `test_group_draw_1_1` → **5.625** |
| Knockout base 1.5, table (§15) | `BASE_POINTS["ko"]`, `SCORE_TABLE["ko"]` | `test_knockout_base_is_1_5` |
| Final base 2.0, table (§16) | `BASE_POINTS["final"]`, `SCORE_TABLE["final"]` | `test_final_draw_2_2` → 5×2.5 |
| Detonator ×2 (§18) | `DETONATOR_FACTOR` | `test_detonator_doubles` |
| −15% group reset (§14) | `engine.apply_group_reset` | `test_group_reset` |
| Prize ladder 23/15/…/4% (§5) | `PRIZE_LADDER`, `engine.prize_split` | `test_prize_split` |
| Futures payouts (§7–9) | `WINNER/SCORER/CINDERELLA_PAYOUT` | re-verified vs PDF; `test_futures::test_payouts_match_rules_counts` |
| Futures EV ranking (§7–10) | `core/decision/futures.py` | `test_futures` (EV = prob×payout, longshots, fighter) |

The three exact-score tables were reconciled two independent ways: (a) the
**diagonal structure** — each table row starts at its draw cell, so the first
value in row *r* is the *r-r* score; (b) the **worked examples** in the PDF
(France 2-1 → 1.5×2.0 = 3.000; 1-1 → 2.25×2.5 = 5.625; final 2-2 = 5). Both agree.

## 2. The decision formula is provably correct

The EV optimizer (`core/decision/ev_optimizer.py`) recommends the scoreline that
maximizes expected points:

```
EV(predict s, direction d) = odds(d) · detonator · [ base·(P(d) − P(s)) + tableMult(s)·P(s) ]
```

This closed form was checked against a **brute-force expectation computed through
the rules-tested scoring engine** over every scoreline, every stage, and both
detonator states. Max discrepancy = **0.001** (pure 3-decimal rounding). So the
recommended pick is, by construction, the expected-points-maximizing choice under
your exact rules — not merely the most-likely score. (Reproduce: the verification
loops in the build notes.)

## 3. Why these models match real-game statistics

- **Dixon-Coles bivariate Poisson** is the standard, peer-reviewed model for
  football scorelines. Plain Poisson under-predicts 0-0/1-0/1-1; the ρ (rho)
  correction in `_dc_tau` fixes exactly those cells — which is also where your
  scoring concentrates value. Time-decay weighting (`xi`) down-weights stale
  results. This is the right shape for a goals distribution.
- **National-team Elo** (eloratings.net methodology) is the accepted strength
  measure for international football, where club-style season stats are sparse.
  Used as a prior, not the anchor.
- **Market de-vig** (`oddsapi.devig`, multiplicative normalization) converts
  bookmaker odds to fair probabilities. **Pinnacle/Betfair** are the sharpest
  markets (~2–3% margin, fast-corrected) and are the best-calibrated free
  probability estimate available — the standard benchmark for model calibration.
- **Blend** leans on the market (0.50) because markets are hard to beat;
  Dixon-Coles (0.30) adds scoreline shape; Elo (0.20) stabilizes sparse data.

### What must be calibrated before you trust it (honest caveats)
- `BLEND_WEIGHTS` are sensible defaults, **not** fitted. Day 3: backtest on recent
  internationals (Brier / log-loss vs the market) and tune.
- `elo.outcome_probs` uses a heuristic draw model; recalibrate `draw_base` on
  historical draw frequencies.
- The goals model is only as good as the historical international results you
  feed `fit_strengths`. Sparse/low-quality data → trust the market weight more.

## 4. Each agent → the real data it collects → how it reasons

| Agent | Real data it pulls | How it turns data into a decision |
|---|---|---|
| **Data** | football-data.org (fixtures, results, status, stage, group); soccerdata/FBref (team & player xG, form); eloratings.net (national Elo) | fits Dixon-Coles attack/defence strengths + supplies Elo; writes to SQLite |
| **Odds** | The Odds API → Pinnacle/Betfair + consensus 1X2 (and totals) | de-vigs to fair probabilities; snapshots the **locked** odds (= scoring multiplier) at T-7m |
| **News/Injury** | web search + API-Football confirmed XI, injuries, suspensions, weather, qualification scenarios | LLM returns **structured** `home/away_goal_delta` (clamped ±0.6) applied to expected goals |
| **Model** | the store (strengths, Elo, market probs, news deltas) | builds the blended score matrix; runs the EV optimizer |
| **Scoring/Standings** | actual results + your submitted picks | applies `score_match`, the −15% reset, prize split, tie-break |

Everything numeric is deterministic Python; the LLM only converts unstructured
news to structured numbers and writes the human-readable card. No arithmetic that
affects points is ever done by the LLM.

## 5. Coverage of the three outputs + what's not yet automated
The system produces three best-practice outputs, **all pure (no standings logic)
by default**: (1) per-game 1X2 + exact score (`ev_optimizer`), (2) futures/overall
bets EV tables (`decision/futures` — built; feed it market-odds or montecarlo
probs), (3) daily side bets (`sidebets`). The
position/standings tilt (`strategy`) is OFF by default — enable later.

Not yet automated (manual in the spreadsheet, or later code):
- Penalty-shootout partial credit (§15c–e, §16c–d) — situational; enter manually
  for the few knockout games that go to penalties, or implement in `scoring`.
- The "fighter" ranking detail (§10) — futures pick is captured; full ranking math
  is a later add.
- Extra-time vs 120' result mapping — handle when wiring knockout results.

## 6. Engineering practices applied
Single source of truth for rules (`config/rules.py`); pure, side-effect-free
scoring/EV functions; dependency-light core with lazy optional imports; unit
tests on every money-critical path (132 passing); model-agnostic LLM router with
fallbacks; stateless idempotent agents for safe parallel/retry; config via env.
