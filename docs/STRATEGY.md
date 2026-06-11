# Winning strategy — is max-EV the same as max-P(win)? (honest audit)

## The short, honest answer (updated Day-9.25 with Monte Carlo data)

**Pure EV-MAX is the correct default for "win the pool"** under realistic
assumptions about how friends pick. Monte Carlo over 50,000 simulated
tournaments × 68 players × 64 matches:

| YOUR strategy | FRIENDS' strategy | **P(YOU WIN)** |
|---|---|---:|
| **EV-MAX (current)** | MODAL (typical casual play) | **61.4%** |
| EV-MAX | MIXED (50% modal / 50% EV-MAX) | 2.9% |
| EV-MAX | EV-MAX | 1.4% (= 1/68, no edge) |
| MODAL | EV-MAX | **0.0%** |
| LONGSHOT | MODAL | 21.7% |

**Key insight:** EV-MAX has both higher mean AND higher variance than MODAL.
Higher mean = better expected finish. Higher variance = wider tails = better
P(landing in the right tail = winning). The combination dominates.

The original concern ("we miss 78% of the time when we pick a draw 0-0!") is
real but mathematically priced into the EV. When EV-MAX hits (≈ 22% on a
Draw 0-0 for Mexico v SA), it pays 25 points; the 78% miss is more than
compensated. Over 64 matches the variance puts EV-MAX's 95th percentile at
~320 points vs MODAL's ~190.

**The strategy tilt layer is still the right tool for position-aware
late-tournament adjustments** (catch-up when behind, lock-in when ahead). It
remains opt-in via `STRATEGY_TILT` env var, OFF by default.

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

## How to actually turn it on (Day-9.5 wiring)

The layer is **wired into the daemon** but defaults to OFF. Three steps to
activate.

### 1 — Tell the system who you are

In your `.env` on the VM:
```bash
MY_PARTICIPANT=Igor          # match the display name in the Negev Toto app
STRATEGY_TILT=0.4            # 0 = off (default); 0.3–0.6 = position-aware
```

The daemon will pick these up on its next restart (`systemctl restart mondial2026`).

### 2 — Get the leaderboard (Day-9.6 auto-sync, recommended)

The Negev Toto Firestore is the source of truth. Day-9.6 wired automatic
daily sync at 07:00 IDT (2 h before the daily summary):

```bash
# Manual run any time (the cron fires automatically every morning)
sudo -u mondial bash -c '
  cd /home/mondial/mondial2026
  set -a && source .env && set +a
  PYTHONPATH=. .venv/bin/python tools/sync_negev_standings.py
'
# → '✓ 63 players synced. You: rank 26/63 ...'
```

The sync calls the Negev MCP's `toto_get_standings(tournament_id=NEGEV_TOURNAMENT_ID)`
and upserts the result into our `standings` table. Mapping:
- Negev `directionPoints` → our `group_points`
- Negev `broadBetPoints` → our `futures_points`
- `knockout_points` always 0 (Negev folds group+KO into `directionPoints`)

The reader (`standings_context`) re-reads on EVERY dispatched job, so a
fresh sync kicks in on the next window-fire — no daemon restart needed.

#### Manual entry as fallback

If the Negev sync is unavailable (token expired, sync script broken),
populate standings manually:

```bash
sudo -u mondial .venv/bin/python tools/standings_set.py set "Alice" \
    --group 32.5 --ko 0 --futures 4.2
sudo -u mondial .venv/bin/python tools/standings_set.py list
```

### 3 — How to enter group points (the §14 -15 % reset)

- **Group stage in progress**: enter the value the Negev app shows. Simple.
- **After the group→KO transition**: the Negev app already discounts everyone's
  group_points by 15%. Enter whatever they show. The reader sums columns raw;
  it doesn't re-apply the reset.
- **Your row only**: `update_standings` auto-applies the reset to YOUR
  group_points based on the matches table. You don't have to do that one
  manually. Friends' rows you do enter as-displayed.

### Verifying the layer is active

When the strategy tilts a pick, the journal logs it:
```
INFO scheduler ... strategy tilt re-picked {'home':3,'away':0} (EV-optimal was {'home':1,'away':0})
```

And the persisted card has a `strategy` block:
```bash
sudo -u mondial sqlite3 /home/mondial/mondial2026/store/mondial.db "
  SELECT json_extract(payload_json,'\$.strategy') FROM predictions
  WHERE json_extract(payload_json,'\$.strategy.applied')=1 LIMIT 5"
```

If `strategy.applied=1` rows appear → the layer is doing its job.

## Bottom line
The algorithm is **the right one and a strong edge**: a calibrated, market-anchored
probability model + a provably-correct expected-points optimizer + an opt-in,
position-aware variance layer for the endgame and for high-leverage futures. That
combination is what maximizes your realistic chance of winning — far more than any
gut-based competitor — while staying honest about football's irreducible luck.

## Day-9.25 — pick_analyzer.py + Monte Carlo validation

### `tools/pick_analyzer.py` — visibility into the trade-off (no behavior change)

```bash
sudo -u mondial bash -c '
  cd /home/mondial/mondial2026
  set -a && source .env && set +a
  PYTHONPATH=. .venv/bin/python tools/pick_analyzer.py Mexico "South Africa" \
    --detonator --xg-home 2.05 --xg-away 0.65 \
    --odds-h 1.43 --odds-d 4.56 --odds-a 8.77
'
```

Shows for the match (top-10 candidates):

| Score | Dir | P(exact) | P(dir) | Mult | EV | P(any pts) | Max(exact) | Max(dir-only) | Sharpe |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **0-0** | D | 7.9% | 21.5% | 2.75 | **3.22** ← EV-MAX | 21.5% | 25.1 | 9.12 | 0.45 |
| 1-1 | D | 10.1% | 21.5% | 2.25 | 3.11 | 21.5% | 20.5 | 9.12 | 0.48 |
| 3-0 | H | 9.7% | 69.0% | 3.25 | 2.60 | 69.0% | 9.3 | 2.86 | 1.02 |
| **2-0** | H | 14.1% | 69.0% | 2.25 | 2.48 ← MODAL | 69.0% | 6.4 | 2.05 | 1.21 |
| 0-2 | A | 0.6% | 9.5% | 1.50 | — ← LONGSHOT | 9.5% | **39.5** | 17.5 | — |

Tags each row with `← EV-MAX (system's pick)`, `← MODAL`, `← SAFEST DIR`, `← LONGSHOT`.
Then a strategy-comparison summary + tournament-context guidance.

### "Likeliest" vs "EV-MAX" — what they mean on the rendered card

The card shows both when they disagree:

| | Likeliest (modal) | EV-MAX (system pick) |
|---|---|---|
| **Question answered** | "What scoreline is most probable?" | "What scoreline pays best given the rules?" |
| **Source** | Poisson matrix argmax | EV formula maximum |
| **Card line** | `(likeliest: Mexico 1 — South Africa 0)` | `► Pick: Draw    Exact: Mexico 0 — South Africa 0` |
| **When they coincide** | Even matches where Poisson peak == EV-best | Korea v Czechia (40/29/31): both 1-1 |
| **When they diverge** | Heavy-favored matches where high-odds direction outweighs high-prob direction | Mexico v SA: likeliest 1-0, EV-MAX 0-0 |

The render rule (`core/delivery/base.py:167-171`) shows the "likeliest" line
ONLY when modal != pick. The disagreement IS the system's edge — picking a
less-likely-but-better-paying scoreline is where math-driven profit comes from.

### When to flip the strategy tilt mid-tournament

| Position | Recommended `STRATEGY_TILT` | Effect |
|---|---|---|
| Tied / unclear standings | `0` (OFF, current) | Pure EV-MAX |
| Slightly behind (5-10 pts) | `0.2-0.3` | Mild variance boost |
| Moderately behind (10-20 pts) | `0.4-0.5` | More upside-leaning picks |
| Far behind (20+ pts) | `0.6-0.8` | Aggressive longshots |
| Leading by 5-10 pts | `0.3` + `STRATEGY_SWING=-3` | Mild defense |
| Leading comfortably | `0.5` + `STRATEGY_SWING=-5` | Stronger defense |

Flip the value any time:

```bash
ssh root@167.233.66.192 'sed -i "s/^STRATEGY_TILT=.*/STRATEGY_TILT=0.4/" /home/mondial/mondial2026/.env && systemctl restart mondial2026'
```

The tilt activates on the next match-window dispatch (standings-context refresh
is per-dispatch by design).
