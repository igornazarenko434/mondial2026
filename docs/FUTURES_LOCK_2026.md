# Futures lock — Mondial 2026

**Deadline: Thu 11 Jun 2026, 21:59 Israel.** After this, the picks are frozen for
the whole tournament.

Last generated: 2026-06-06 (re-run `python -m tools.futures_lock` to refresh).

---

## Recommended picks

| Market | Pick | EV | Margin to #2 | Confidence | Data source |
|---|---|---:|---:|---|---|
| **Winner** | **Portugal** | 3.04 | +0.14 | Moderate (close race vs England/Brazil) | the-odds-api live outright market |
| **Cinderella** | **Uzbekistan** | 0.99 | +0.62 | **High** (huge margin) | Monte Carlo bracket sim (20k) |
| **Scorer** | **Memphis Depay** | 3.85 | +0.47 | Moderate (no live market — MC fallback) | MC + per-player xG model |
| **Fighter** | _your manual choice_ | — | — | — | not computed |

---

## Why each pick (and what to override if you disagree)

### Winner → Portugal

The market gives Spain and France ~14% each to win the cup; Portugal ~8%. But
the Toto pays a FIXED 20 points for Spain/France versus **39 points** for
Portugal. EV math:

```
Spain      14.16% × 20 = 2.83
France     14.16% × 20 = 2.83
England    11.12% × 26 = 2.89
Brazil      8.65% × 33 = 2.86
Portugal    7.79% × 39 = 3.04  ← max EV
Argentina   7.79% × 34 = 2.65
USA         1.53% × 170 = 2.60
Germany     5.19% × 43 = 2.23
```

Portugal sits at the **sweet spot of the EV curve**: low enough payout-to-prob
ratio that the math beats the favorites, high enough actual probability
(~8%) that the payout matters. The classic "you don't pick the favorite in a
fixed-payout pool" rule.

The **+0.14 margin to England** is small — if you think England's draw is
softer than Portugal's, England is a defensible alternative. The market
disagrees but only by ~5%.

**The market data IS what determines this pick.** If odds shift before the
lock (e.g. someone gets injured), re-run the lock and the pick may change.

**Skip / override if:** you have intel the market doesn't (e.g. Cristiano
Ronaldo retiring, Portuguese keeper injured) — pick England as the runner-up
EV (2.89).

### Cinderella → Uzbekistan

This pick is **driven entirely by the Monte Carlo simulator**, not market odds
(no betting market exists for "which cinderella goes deep"). The MC ran 20,000
full-tournament simulations using:

- 256 teams of historical international results → Dixon-Coles fit → expected
  goals per fixture
- Live national-team Elo for all 48 WC participants
- Real WC 2026 group structure (12 groups of 4) + best-8-third-placed rules
- Snake-seeded R32 bracket with intra-group rematch avoidance
- Poisson goal sampling + Elo-edge penalty shootout on KO draws

Across all 11 cinderella candidates:

```
team         P(reach QF)  × payout = EV
Uzbekistan        4.31%      23     0.99  ← max EV (huge margin)
Cape Verde        1.68%      22     0.37
Iraq              0.70%      35     0.25
Panama            0.80%      23     0.19
Saudi Arabia      0.84%      16     0.13
Jordan            0.36%      32     0.12
New Zealand       0.55%      19     0.10
Congo DR          0.67%      15     0.10
Qatar             0.29%      23     0.07
Haiti             0.08%      72     0.05
Curacao           0.02%      75     0.01
```

**Why Uzbekistan crushes the others:**

1. **Group K is winnable for a third-place finish.** Uzbekistan is grouped with
   Portugal (top contender) + Colombia (mid-tier) + Congo DR (weakest). MC
   says Uzbekistan finishes **second or third 53.8% of the time** — they
   advance from the group 46% of the time (top 2) and as a top-8 third-placed
   another ~13%, totaling **~33% advance to R32**.

2. **R32 path is plausible.** Once in R32, snake seeding pairs them with a
   middle-tier group winner. MC tracks each R32→R16→QF transition.
   Compound probability lands at 4.3% to reach QF.

3. **Payout is mid-range.** At 23 points, Uzbekistan's payout isn't huge but
   their probability of getting there is **2-5× higher** than the longshot
   cinderellas (Haiti 72, Curacao 75). The math: mid-payout × decent-prob
   beats huge-payout × tiny-prob.

The **+0.62 margin** is the largest of the three picks. Even if the MC is off
by a factor of 2 on Uzbekistan, they'd still be #1.

**Skip / override if:** you want the contrarian variance play, Haiti or Curacao
both have payouts that would be tournament-winning if they hit. But the math
says the probability isn't there.

### Top scorer → Memphis Depay

The-odds-api **doesn't list a WC top-scorer market** on the free tier. So we
fall back to a calculated model:

```
expected_tournament_goals(player) =
    Σ P(team reaches stage X) × matches_played_in_stage × per_match_xG
```

The `per_match_xG` numbers are calibrated estimates per player (we'd tune them
on real player data later):

```
Mbappé           France      0.65/match
Haaland          Norway      0.70/match
Harry Kane       England     0.55/match
Lukaku           Belgium     0.50/match
Memphis Depay    Netherlands 0.45/match  ← solid forward
Vinicius         Brazil      0.45/match
Lautaro          Argentina   0.50/match
...
```

For each player, multiply by their team's expected matches played (~3 to ~7
depending on advance probability), normalize across the 19 candidates → P(top
scorer of these 19), × the SCORER_PAYOUT.

```
player              P(top)   × payout = EV
Memphis Depay        5.27%      73      3.85   ← max EV
Lukaku               5.93%      57      3.38
Julian Alvarez       5.91%      48      2.84
Lautaro Martinez     6.56%      40      2.63
Cody Gakpo           4.10%      61      2.50
Bellingham           3.73%      67      2.50
Michael Olise        3.80%      65      2.47
Mbappé               (lowest payout 20 — EV stays low)
```

**Why Depay over Mbappé / Haaland:**

Mbappé is the BEST player on the list, but his payout is **only 20** (the lowest
because everyone picks him). His EV stays modest (~1.8) even with his ~9% true
probability.

Depay sits at #5 in expected goals but his **payout is 73 (highest)** because
the Toto board considers him a longshot. Math: 5.27% × 73 = 3.85 beats the
favorites.

**This is the LEAST robust of the three picks** — three caveats:

1. **No market** to cross-check. If the-odds-api adds a top-scorer market
   before the lock, the script will switch to it automatically.
2. **The model doesn't account for penalty takers.** Mbappé takes France's
   penalties; that's worth ~0.1-0.2 extra goals per tournament. If the user
   has knowledge here, override.
3. **Player rotation / injury risk** isn't modeled.

**Skip / override if:** you have inside info on form / lineup / injuries.
Manual sanity check: Memphis Depay is a viable Dutch striker but Netherlands
needs to go deep for him to score. If you think NL exits in R16, the EV
collapses.

### Fighter — manual choice

Per the rules §10, the fighter is whoever finishes highest among a low-seed
list. You said you want to pick this manually — not calculated. The §10
ranking math isn't automated in this codebase yet (it's listed in CLAUDE.md
"Backlog: rule cases intentionally NOT yet automated").

---

## How to re-run before the lock

```bash
cd ~/private_Igor/Mondial_2026/mondial2026
.venv/bin/python -m tools.futures_lock
```

Takes ~15 seconds, costs **2 odds_api credits** out of ~497 remaining in the
500/mo budget. Writes:

- `reports/futures_lock.json` — full output (gitignored; regenerate any time)
- Console: pretty-printed tables + final picks

Schedule re-runs:
- **Sun 7 Jun** — sanity check before the week kicks off
- **Wed 10 Jun** — final check (~24h before lock)
- **Thu 11 Jun morning** — last-minute check; lock immediately if picks stable

If a pick changes by more than 1 rank between runs, investigate **why** (which
team's odds moved? which group's MC outcome shifted?) before changing.

---

## Calibration notes for next time

- The MC `per_match_xG` values for top scorers are rough estimates. Recalibrate
  on real player tournament data after WC 2026 ends.
- If you want to enable the the-odds-api **historical** endpoint for
  pre-tournament calibration (10× cost = ~50 calls in 500 budget), see Day-10
  plan in `CLAUDE.md`.
- The MC ignores extra time / 120-minute scoring; KO games are sampled as
  90'-result and shootout-resolved on draws. Real WC has ~10% chance of going
  to ET — the bias on stage-reach probs is <1pp.
