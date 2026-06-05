# Winning strategy — is max-EV the same as max-P(win)? (honest audit)

## The short, honest answer
**No.** The EV optimizer maximizes your *expected total points*. Winning a
top-heavy prize pool (23/15/12.5…%) is a **different objective**: maximize
P(finishing 1st/in the money). Established bracket-pool and DFS-tournament theory
is unambiguous:

> Picking all favourites / pure expected value gets you a *min-cash*, not first
> place. To win, you must **differentiate from the field** and **tune your
> variance to your standing and the field's chalkiness.**

So our per-game EV pick is the **correct foundation and the best single objective
for most of the tournament** — and it will almost certainly beat friends picking
by gut. But to truly maximize the chance of *winning*, we add a thin layer.

## What the strategy layer does (`core/decision/strategy.py`)
It chooses **among the top-EV candidates only** (never reckless), nudged by your
position:
- **Behind, time short** → take the higher-variance / longer-odds / rarer-score
  near-optimal pick. You need points others won't have.
- **Ahead** → protect: prefer the safer, higher-probability pick (hedge toward the
  field) so one bad night can't be leapfrogged.
- **Neutral / `STRATEGY_TILT=0`** → returns the pure-EV pick unchanged.

`risk_pressure(your_points, leader_points, games_left, second_points)` → a value in
[-1,1] (>0 behind, <0 ahead). The pick is `EV + pressure·tilt·upside`. Default
`tilt=0` (opt-in), tune `STRATEGY_TILT` (0.3–0.6 moderate). Standings come from the
scoring/runs layer — **no opponent pick data required.**

## Where the biggest leverage actually is
1. **Futures (§7–10).** Longshot-weighted points (USA 170, Curaçao 75, Depay 73)
   are the highest-variance, highest-leverage single decisions of the whole pool.
   The EV table ranks them; for *winning*, a slightly contrarian futures pick (one
   the field won't have) is often the differentiator. This is where to spend your
   variance budget first.
2. **The −15% group reset (§14)** compresses the field → it's a built-in comeback
   point; don't over-commit to variance before it.
3. **Detonator games (×2, §18)** are variance amplifiers — lean into them when
   behind, treat them carefully when protecting a lead.
4. **Exact scores on longer-odds outcomes** — already the core EV edge; the
   strategy layer extends it when you need to catch up.

## Honest limits (why we keep it modest, not maxed)
- Football is noisy and the pool is small; **over-tilting to game theory adds risk
  without reliable reward.** The literature's "go contrarian" is strongest in
  *large* fields — a friends' pool is small, so moderate tilt is right.
- We can't see opponents' picks programmatically, so field-modeling is heuristic
  (favourites = chalk). If the group's app *does* show others' picks, feed them in
  to compute true differentiation/leverage — that's the one upgrade that would make
  this provably optimal.
- The EV engine's accuracy still depends on calibrated model+market inputs
  (docs/VERIFICATION.md). Strategy can't fix bad probabilities — it allocates
  variance given good ones.

## How it's connected (and how to turn it on later)
The layer is **wired into the pipeline but dormant by default**:
- `orchestrator/pipeline.process_match(..., strategy_context=None, strategy_tilt=None)`
  applies `strategy.recommend_to_win(card, context, tilt)` as a post-step after the
  card is built. With no context / `STRATEGY_TILT=0` it's a **no-op** → pure EV.
- To enable mid-tournament: build the context once per run with
  `store.repo.standings_context(conn, me="Igor")` (reads the `standings` table +
  games-left from the calendar; returns `None` if standings aren't populated, so it
  safely stays off), then pass it + a tilt (0.3–0.6) to `process_match`.
- The chosen card carries a `strategy` block (`applied`, `deviated_from_ev`,
  `ev_optimal_score`) so you always see when/why it deviated from the EV pick; the
  pipeline logs the deviation.

### Fallbacks & edge cases (all handled)
- **Fallback-safe:** `recommend_to_win` is wrapped so *any* error or missing field
  (no `ranked_alternatives`, bad context, NaN) returns the **original EV pick** —
  strategy can only refine, never break, a card.
- **Tilt clamped** to [0,1]; tilt 0 or no context → EV pick unchanged.
- **No standings data** → `standings_context` returns `None` → no-op.
- **Games-left = 0 / you're the leader by a lot** → pressure → 0 or negative
  (protect), never forces a reckless pick; it only ever chooses among the top-K EV
  candidates.

## Bottom line
The algorithm is **the right one and a strong edge**: a calibrated, market-anchored
probability model + a provably-correct expected-points optimizer + an opt-in,
position-aware variance layer for the endgame and for high-leverage futures. That
combination is what maximizes your realistic chance of winning — far more than any
gut-based competitor — while staying honest about football's irreducible luck.
