"""Pre-tournament futures bets: EV ranking matches the rules payouts exactly."""
from core.decision.futures import (implied_probs, ev_table, rank_winner,
                                    rank_scorer, rank_cinderella, rank_fighter,
                                    recommend_futures)
from config.rules import WINNER_PAYOUT, SCORER_PAYOUT, CINDERELLA_PAYOUT


def test_payouts_match_rules_counts():
    # guards against accidental edits to the rule tables
    assert WINNER_PAYOUT["United States"] == 170 and WINNER_PAYOUT["Spain"] == 20
    assert len(WINNER_PAYOUT) == 10 and len(SCORER_PAYOUT) == 19 and len(CINDERELLA_PAYOUT) == 11
    assert SCORER_PAYOUT["Memphis Depay"] == 73 and CINDERELLA_PAYOUT["Curacao"] == 75


def test_implied_probs_normalised():
    p = implied_probs({"Spain": 6.0, "France": 6.5, "Brazil": 9.0, "USA": 200.0})
    assert abs(sum(p.values()) - 1.0) < 1e-9
    assert p["Spain"] > p["USA"]               # shorter odds → higher prob


def test_ev_ranks_by_prob_times_payout():
    # favourite (high prob, low payout) vs longshot (low prob, huge payout)
    probs = {"Spain": 0.18, "United States": 0.01}     # Spain pays 20, USA pays 170
    tbl = rank_winner(probs)
    ev = {r["option"]: r["ev"] for r in tbl}
    assert ev["Spain"] == round(0.18 * 20, 3)          # 3.6
    assert ev["United States"] == round(0.01 * 170, 3) # 1.7
    assert tbl[0]["option"] == "Spain"                 # higher EV ranks first


def test_longshot_can_win_on_ev():
    probs = {"Spain": 0.05, "United States": 0.03}     # USA EV 5.1 > Spain EV 1.0
    assert rank_winner(probs)[0]["option"] == "United States"


def test_missing_option_is_zero_ev_not_error():
    tbl = rank_scorer({"Mbappe": 0.2})                 # only one provided
    assert tbl[0]["option"] == "Mbappe"
    assert all(r["ev"] == 0.0 for r in tbl if r["option"] != "Mbappe")


def test_fighter_ranks_by_deep_run():
    f = rank_fighter({"Curacao": 0.04, "Haiti": 0.02, "Jordan": 0.10})
    assert f[0]["option"] == "Jordan"


def test_recommend_futures_all_markets():
    out = recommend_futures({
        "winner": {"Spain": 0.18, "Brazil": 0.10},
        "scorer": {"Mbappe": 0.2, "Harry Kane": 0.12},
        "cinderella": {"Jordan": 0.08, "Curacao": 0.03},
        "fighter": {"Jordan": 0.10, "Curacao": 0.04},
    })
    assert out["picks"]["winner"] == "Spain"
    assert out["picks"]["scorer"] == "Mbappe"
    assert set(out["picks"]) == {"winner", "scorer", "cinderella", "fighter"}
