"""Pin the Day-7 payout-table audit so we catch drift before any MC run.

The PDF rules (= config.rules.{WINNER,CINDERELLA,SCORER}_PAYOUT) and the
groups CSV must agree on:
  - every winner / cinderella candidate is in the WC 48
  - every team key is the canonical form (so MC + market joins line up)
  - every cinderella candidate is also flagged in the CSV
  - every top-scorer player's nation is in the WC 48
"""
from __future__ import annotations
import csv
from config.rules import WINNER_PAYOUT, CINDERELLA_PAYOUT, SCORER_PAYOUT
from core.data.teams import normalize


def _wc_truth() -> tuple[set[str], set[str]]:
    """Returns (all_48_canonical_teams, cinderella_flagged_in_csv)."""
    with open("data/wc2026_groups.csv") as f:
        rows = list(csv.DictReader(f))
    return (
        {normalize(r["team"]) for r in rows},
        {normalize(r["team"]) for r in rows
         if "cinderella" in (r.get("confederation_seed_note", "") or "")},
    )


# Player → national team (the team they'd score for in WC 2026).
_PLAYER_NATION = {
    "Mbappe": "France", "Harry Kane": "England", "Messi": "Argentina",
    "Haaland": "Norway", "Mikel Oyarzabal": "Spain", "Lamine Yamal": "Spain",
    "Cristiano Ronaldo": "Portugal", "Ousmane Dembele": "France",
    "Vinicius": "Brazil", "Lautaro Martinez": "Argentina",
    "Raphinha": "Brazil", "Kai Havertz": "Germany",
    "Julian Alvarez": "Argentina", "Romelu Lukaku": "Belgium",
    "Igor Thiago": "Brazil", "Cody Gakpo": "Netherlands",
    "Michael Olise": "France", "Jude Bellingham": "England",
    "Memphis Depay": "Netherlands",
}


def test_every_winner_candidate_is_in_wc_2026():
    all_teams, _ = _wc_truth()
    missing = [t for t in WINNER_PAYOUT if normalize(t) not in all_teams]
    assert not missing, f"WINNER candidates not in WC 2026: {missing}"


def test_every_winner_key_is_canonical():
    drift = [t for t in WINNER_PAYOUT if normalize(t) != t]
    assert not drift, ("WINNER_PAYOUT keys must already be canonical "
                       f"(MC + market joins use the same key): {drift}")


def test_every_cinderella_candidate_is_in_wc_2026():
    all_teams, _ = _wc_truth()
    missing = [t for t in CINDERELLA_PAYOUT if normalize(t) not in all_teams]
    assert not missing, f"CINDERELLA candidates not in WC 2026: {missing}"


def test_every_cinderella_key_is_canonical():
    drift = [t for t in CINDERELLA_PAYOUT if normalize(t) != t]
    assert not drift, f"CINDERELLA_PAYOUT keys not canonical: {drift}"


def test_csv_cinderella_flags_match_payout_table():
    """The CSV's `cinderella_listed` annotation must be a SUPERSET of the
    payout-table keys (the rules PDF is canonical; CSV must reflect it)."""
    _, csv_flagged = _wc_truth()
    payout_keys = {normalize(t) for t in CINDERELLA_PAYOUT}
    missing_flag = payout_keys - csv_flagged
    assert not missing_flag, (
        "These cinderella payout candidates aren't flagged in the CSV "
        f"— update data/wc2026_groups.csv: {sorted(missing_flag)}")


def test_every_scorer_candidate_has_known_nation():
    unknown = [p for p in SCORER_PAYOUT if p not in _PLAYER_NATION]
    assert not unknown, (
        "Add these players to _PLAYER_NATION (tests + tools.futures_lock): "
        f"{unknown}")


def test_every_scorer_nation_is_in_wc_2026():
    all_teams, _ = _wc_truth()
    dead = [(p, _PLAYER_NATION[p]) for p in SCORER_PAYOUT
            if normalize(_PLAYER_NATION[p]) not in all_teams]
    assert not dead, (
        "Top-scorer candidates whose nation DIDN'T qualify (would be a dead "
        f"pick): {dead}")
