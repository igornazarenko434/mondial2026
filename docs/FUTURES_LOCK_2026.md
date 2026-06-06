# Futures lock — Mondial 2026 (UPDATED 2026-06-06)

**Deadline: Thu 11 Jun 2026, 21:59 Israel.** After this, picks are frozen for
the whole tournament.

**Goal:** *win* the friends' Toto pool (not just place in the top half).
Top-heavy prizes (23% / 15% / 12.5%) means we balance EV with differentiation.

---

## Recommended picks (updated after web-data cross-check)

| Market | Pick | EV | Margin to #2 | Contrarian alternative | Data source |
|---|---|---:|---:|---|---|
| **Winner** | **Portugal** | 3.04 | +0.14 | Argentina (2.65) for more variance | the-odds-api live (Pinnacle/Betfair) |
| **Cinderella** | **Uzbekistan** | 0.99 | **+0.62** | none — pick is dominant | MC bracket sim (20k) |
| **Scorer** | **Mbappé** | 3.39 | +0.42 | **Bellingham** (EV 2.88) for high differentiation | Market-prior research (web) + MC team factor |
| **Fighter** | _your manual choice_ | — | — | — | not computed |

### Changelog vs. the first run (1 hour ago)

| Pick | Before | After | Why |
|---|---|---|---|
| Winner | Portugal 3.04 | Portugal 3.04 | unchanged — cross-checked against ESPN/SI/FoxSports market data |
| Cinderella | Uzbekistan 0.99 | Uzbekistan 0.99 | unchanged — huge margin to #2 is bulletproof |
| **Scorer** | **Depay 3.85** | **Mbappé 3.39** | **Bug found**: my MC fallback overstated less-famous players. Replaced with web-research market prior → Mbappé tops the corrected table. |

---

## Data sources — current as of 2026-06-06 12:55 Israel

| Source | Used for | Freshness | Confidence |
|---|---|---|---|
| `the-odds-api` `soccer_fifa_world_cup_winner` | WINNER probabilities | Live fetch (2 credits) | ✅ High — sharp Pinnacle + Betfair Exchange shortest odds |
| Web research (FOX/SI/ESPN/Goal.com, Jun 2026) | WINNER cross-check; SCORER market prior | Today's headlines | ✅ Medium-high — multi-source consensus |
| `the-odds-api` top-scorer market | (none — not listed on free tier) | n/a | ❌ Falls back to model |
| Monte Carlo (20k sims) | CINDERELLA P(reach QF+), SCORER team-factor | Live (DC fit + Elo, both daily-cached) | ✅ Medium — model-based |
| `martj42/international_results` (4067 rows ÷ 4y) → DC fit | MC's per-fixture expected goals | 24h cache | ✅ High — academic standard |
| `eloratings.net/World.tsv` | Penalty-shootout edge, MC defensive prior | 24h cache | ✅ High — sharpest national-team rating |

---

## Why each pick (and the pool-win calculus)

### Winner → Portugal (EV 3.04, +0.14 margin)

**The EV curve:**

| Team | Market P(win) | Toto payout | EV | Pool popularity (est.) |
|---|---:|---:|---:|---|
| Spain | 14.16% | 20 | 2.83 | **30-40% of friends pick** |
| France | 14.16% | 20 | 2.83 | **20-30% of friends pick** |
| **Portugal** | **7.79%** | **39** | **3.04** | 10-15% (the smart-money pick) |
| England | 11.12% | 26 | 2.89 | 15-20% |
| Brazil | 8.65% | 33 | 2.86 | 10-15% |
| Argentina | 7.79% | 34 | 2.65 | 10-15% |
| USA | 1.53% | 170 | 2.60 | <2% (longshot) |
| Germany | 5.19% | 43 | 2.23 | 5-10% |

**Why Portugal is the EV-optimal AND pool-win optimal pick:**
- ✅ **#1 by pure EV** — the math is the math
- ✅ **Moderately contrarian** — only ~10-15% of pool likely picks Portugal, vs Spain/France's combined 50-70%
- ✅ **Real probability** — at 7.79%, Portugal genuinely could win (not a Hail Mary like USA at 1.53%)

**Web cross-check (Jun 2026):** Spain +450 (~18% retail), France +475 (~17%),
Portugal +850 (~10.5%, shortened from 10-1 — public moving to Portugal).
Our Pinnacle/Betfair fetch (the sharpest market) gave Portugal 7.79% — the
sharper number, consistent with the broader market.

**Pool-win sharpening (alternatives if Portugal feels too consensus):**
- **Argentina (EV 2.65, payout 34)**: harder upset; rare pick in pool — high differentiation
- **USA (EV 2.60, payout 170)**: max-variance Hail Mary — almost never wins, but if it does, you crush every friend who picked chalk

**Override if:** Cristiano Ronaldo gets injured, or Spain's odds drift longer (currently both books have Spain favored). Re-run the lock to refresh.

### Cinderella → Uzbekistan (EV 0.99, **+0.62 margin** — strongest pick)

**The full table:**

| Team | MC P(QF+) | Payout | EV |
|---|---:|---:|---:|
| **Uzbekistan** | **4.31%** | **23** | **0.99** ← max |
| Cape Verde | 1.68% | 22 | 0.37 |
| Iraq | 0.70% | 35 | 0.25 |
| Panama | 0.80% | 23 | 0.19 |
| Saudi Arabia | 0.84% | 16 | 0.13 |
| Jordan | 0.36% | 32 | 0.12 |
| New Zealand | 0.55% | 19 | 0.10 |
| Congo DR | 0.67% | 15 | 0.10 |
| Qatar | 0.29% | 23 | 0.07 |
| Haiti | 0.08% | 72 | 0.05 |
| Curacao | 0.02% | 75 | 0.01 |

**Why the MC gives Uzbekistan +0.62 over Cape Verde:**

1. **Group K is winnable.** Portugal + Colombia + Congo DR + Uzbekistan. Portugal is strong; Colombia mid-tier; Congo DR weak. The MC plays the full round-robin and finds Uzbekistan finishing 2nd or 3rd in ~46% of sims, then qualifying as a top-8 third-placed team frequently → **32.7% advance to R32**.

2. **R32 opponent realistic.** Snake seeding pairs them with a mid-tier group winner. Their R32 win rate ~25% × P(advance) ≈ ~8% reach R16. P(reach QF) compounds to ~4.3%.

3. **Mid-payout × decent-prob beats huge-payout × tiny-prob.** Haiti (72) and Curaçao (75) pay more but the MC says they basically never reach QF.

4. **No public pick competition.** Most friends will look at the highest payout (Curaçao 75, Haiti 72) and pick those for variance — they don't see Uzbekistan's hidden edge. **High differentiation.**

**This is the most robust of the three picks** — both EV-optimal AND pool-win-optimal. Even if the MC overstates Uzbekistan by 50%, they'd still be #1.

### Top scorer → Mbappé (EV 3.39, +0.42 margin)

**Original pick was wrong** — my model fallback for scorer was naive (used per-player xG × team stage probs without a market prior). It over-credited less-famous players like Memphis Depay. The web showed actual market odds, so I rewrote the fallback to use a **market-prior + MC team-factor hybrid**.

**Web-research market data (de-vigged, Jun 2026):**

| Player | Implied P(top scorer) | Payout | EV | Pool popularity |
|---|---:|---:|---:|---|
| **Mbappé** | **17.0%** | 20 | **3.39** | 40-60% of friends pick |
| Harry Kane | 14.1% | 21 | 2.97 | 15-25% |
| Jude Bellingham | 4.3% | 67 | 2.88 | <5% ← contrarian sweet spot |
| Julian Álvarez | 5.7% | 48 | 2.74 | 5-10% |
| Lautaro Martínez | 5.7% | 40 | 2.28 | 5-10% |
| Lamine Yamal | 7.2% | 30 | 2.15 | 5-10% |
| Cody Gakpo | 3.5% | 61 | 2.13 | <5% |
| Vinícius | 5.1% | 39 | 2.00 | 5-10% |
| Memphis Depay | 1.6% | 73 | 1.17 | <2% |

**Mbappé is the pure-EV pick:** 17% × 20 = 3.39. His Toto payout is the lowest because he's the obvious favorite, but his probability is so high that the math still wins.

**Pool-win consideration:**

Mbappé wins ~17% of tournaments. If Mbappé wins:
- 50% of pool also picked him → you're tied with many → small pool-win equity
- The friends who picked him won't separate from you

**Contrarian alternative: Bellingham (EV 2.88, payout 67).** If Bellingham
wins (~4.3%), almost nobody else picked him → you uniquely score 67 points →
huge pool-win equity. Trade-off: 17% vs 4% probability — Mbappé is 4× more
likely to actually be the top scorer, BUT only ~2× more pool-win equity
because of crowding. **Bellingham is a defensible aggressive pick** if you
want max differentiation.

**Recommendation by risk tolerance:**

- **Safe (recommended baseline)**: Mbappé — pure EV, highest expected
  points
- **Contrarian (for "I must win the pool")**: Bellingham — moderate EV, much
  higher P(uniquely winning if hit)
- **Hail Mary**: skip Memphis Depay (real probability too low; my first pick
  was a model artifact)

### Fighter — your manual choice (per your instruction)

The fighter pick rules (§10) need ranking math we haven't automated. Pick
based on your sense of which low-seed team can finish highest. Common choices:
Iceland, Senegal-types — countries that historically punch above their seed.

---

## Math best-practices review

Per your check-yourself ask:

| Calculation | Approach | Best practice? |
|---|---|---|
| **Winner EV** | de-vigged Pinnacle/Betfair market × Toto payout | ✅ Standard sharps-market-implied EV |
| **De-vig** | Multiplicative normalization (`implied_probs`) | ✅ Standard; also tested vs additive |
| **Cinderella MC** | 20k tournaments, real bracket, Poisson goals + Elo penalty | ✅ Standard sports-modeling; could improve with real FIFA bracket template once published |
| **Scorer fallback** | **NEW**: market prior × sqrt(MC deep-run factor), renormalized | ✅ Hybrid is sharper than pure-MC or pure-prior |
| Per-match xG values | Calibrated to real tournament patterns (Mbappé 0.85, recalibrated from naive 0.65) | ✅ Improved from prior; would be ideal to fit on real WC2022 historical data |
| Bracket structure | Snake seeding with intra-group rematch avoidance | 🟡 Approximation — real FIFA template not yet published for the 8-third-placed permutations |
| Sample size | 20,000 simulations | ✅ 95% CI for P=0.05 is ±0.003 — tight enough |
| DC correction in MC | OFF (used raw Poisson for speed) | 🟡 Bias on stage-reach probs <1pp — acceptable; would help slightly for finer cinderella ranking |

---

## How to re-run before the lock

```bash
cd ~/private_Igor/Mondial_2026/mondial2026
.venv/bin/python -m tools.futures_lock
```

Takes ~10 seconds. Costs **2 odds_api credits** out of ~496 remaining.

Re-fetches:
- Live winner outright market (sharpest decimal across Pinnacle + Betfair)
- Probes top-scorer market (currently not listed on free tier)
- Re-runs MC if cache stale (24h)

Writes:
- `reports/futures_lock.json` (gitignored — regenerate any time)
- Console pretty-print of all three EV tables + final picks

**Re-run schedule recommendation:**
- **Sun 7 Jun** — sanity check
- **Wed 10 Jun morning** — pre-lock review
- **Thu 11 Jun morning** — last refresh before locking

Watch for: news that shifts a top team's market odds (injury, suspension), or
a top-scorer market becoming available (it would auto-replace the fallback).

---

## Open improvements (out of scope for this lock — for next tournament)

- Use the-odds-api **historical** endpoint (10× credit cost) to backtest the
  MC's stage-reach probabilities against WC 2022 (see CLAUDE.md Day 10).
- Pull real per-player xG/match from FBref or Understat instead of hand-calibrated
  values in `_PLAYER_TEAM_XG`.
- Add a proper FIFA 2026 R32 bracket template once published (currently snake
  seeded).
- Add penalty-taker boost for scorers (Mbappé/Ronaldo/Kane all take penalties).
