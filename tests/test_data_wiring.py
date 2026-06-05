"""Day 2 + Day 3 wire-up tests: _fetch_eloratings + _fetch_live.

Both fetchers accept an injectable http_get(url)->text so tests stay offline.
We verify the parsing + filtering + name resolution, not network behavior.
"""
from __future__ import annotations
from datetime import date, timedelta

from core.data.soccerdata_io import _fetch_eloratings, national_team_elo, elo_of
from core.data.results_io import _fetch_live, historical_results


# ---------- Day 2: eloratings.net TSV → (team, elo) rows ----------

_SAMPLE_TSV = "\n".join([
    # row col0=row_num, col1=rank, col2=code, col3=elo, ...extra cols
    "1\t1\tES\t2155\textra\tcols\tare\tignored",
    "2\t2\tAR\t2113\t",
    "3\t3\tFR\t2062",
    "4\t4\tEN\t2020",
    "5\t5\tBR\t1988",
    "6\t6\tXX\t1700",                       # unknown code -> skipped
    "7\t7\tBA\tnot_a_number",               # bad rating -> warned + skipped
    "8\t8\tSQ\t1770",                       # Scotland (football-specific code)
    "9\t9\tNI\t1334",                       # Northern Ireland (NOT Nicaragua)
])


def test_fetch_eloratings_parses_tsv_and_resolves_codes():
    rows = _fetch_eloratings(http_get=lambda url: _SAMPLE_TSV)
    by_team = dict(rows)
    assert by_team["Spain"] == 2155.0
    assert by_team["Argentina"] == 2113.0
    assert by_team["France"] == 2062.0
    assert by_team["England"] == 2020.0
    assert by_team["Brazil"] == 1988.0
    assert by_team["Scotland"] == 1770.0
    assert by_team["Northern Ireland"] == 1334.0
    # XX is not in the code map -> dropped silently
    assert all(t for t, _ in rows)
    assert "Bosnia-Herzegovina" not in by_team  # bad rating row excluded


def test_fetch_eloratings_empty_tsv_returns_empty():
    rows = _fetch_eloratings(http_get=lambda url: "")
    assert rows == []


def test_national_team_elo_runs_through_normalize_and_elo_of():
    """End-to-end: fetched rows -> cached dict -> elo_of() lookup."""
    elo = national_team_elo(
        fetch=lambda: [("Korea Republic", 1758), ("Cabo Verde", 1576), ("turkey", 1906)],
        cache_path=None,
    )
    # normalize() canonicalizes aliases
    assert elo["South Korea"] == 1758.0
    assert elo["Cape Verde"] == 1576.0
    assert elo["Türkiye"] == 1906.0
    # elo_of() finds them via normalize() too
    assert elo_of(elo, "Korea Republic") == 1758.0
    # missing teams fall back to neutral baseline (1500), not crash
    assert elo_of(elo, "Nowhereistan") == 1500.0


# ---------- Day 3: martj42 CSV → historical results ----------

_RECENT_DATE = (date.today() - timedelta(days=30)).isoformat()
_OLD_DATE = (date.today() - timedelta(days=365 * 6)).isoformat()
_SAMPLE_CSV = "\n".join([
    "date,home_team,away_team,home_score,away_score,tournament,city,country,neutral",
    f"{_RECENT_DATE},Spain,France,1,2,Friendly,Madrid,Spain,FALSE",
    f"{_RECENT_DATE},Korea Republic,Japan,0,0,AFC,Seoul,South Korea,FALSE",
    f"{_OLD_DATE},Brazil,Argentina,3,1,Copa,Rio,Brazil,FALSE",  # outside 4y window
    f"{_RECENT_DATE},Mexico,South Africa,NA,NA,Friendly,Mexico City,Mexico,FALSE",  # void score
    "2026-06-27,Jordan,Argentina,NA,NA,FIFA World Cup,Arlington,United States,TRUE",  # future
])


def test_fetch_live_filters_window_and_void_scores():
    rows = _fetch_live(http_get=lambda url: _SAMPLE_CSV)
    # Two recent rows with real scores; old row + NA rows dropped.
    assert len(rows) == 2
    home_pairs = {(r["home"], r["away"]) for r in rows}
    assert ("Spain", "France") in home_pairs
    assert ("Korea Republic", "Japan") in home_pairs
    # types are coerced
    spain = next(r for r in rows if r["home"] == "Spain")
    assert spain["home_goals"] == 1 and spain["away_goals"] == 2


def test_fetch_live_respects_explicit_window():
    rows = _fetch_live(http_get=lambda url: _SAMPLE_CSV, window_years=10)
    # 10y window keeps the old Brazil-Argentina row too
    assert any(r["home"] == "Brazil" and r["away"] == "Argentina" for r in rows)


def test_historical_results_normalizes_team_names():
    """End-to-end: CSV rows -> historical_results() -> normalized teams + days_ago."""
    rows = historical_results(fetch=lambda: [
        {"home": "Korea Republic", "away": "USA", "home_goals": 1,
         "away_goals": 1, "date": _RECENT_DATE},
        {"home": "Cabo Verde", "away": "Cote d'Ivoire", "home_goals": 0,
         "away_goals": 2, "date": _RECENT_DATE},
        # missing goals -> dropped
        {"home": "X", "away": "Y", "home_goals": None, "away_goals": 0,
         "date": _RECENT_DATE},
    ], cache_path=None)
    teams = {(r["home"], r["away"]) for r in rows}
    assert ("South Korea", "United States") in teams
    assert ("Cape Verde", "Ivory Coast") in teams
    assert all(r["days_ago"] >= 0 for r in rows)
    assert len(rows) == 2


# ---------- alias regression: cross-source spellings -> canonical ----------

# Each entry: (source_label, raw_spelling, expected_canonical_name).
# Sources verified live (Jun 2026):
#   - football-data.org: GET /v4/competitions/WC/matches
#   - martj42/international_results CSV (last 4y window)
#   - groups CSV ground truth: data/wc2026_groups.csv
# If any source ever changes its spelling, add the new raw form here and to
# core/data/teams.py::_ALIASES so the model never silently misses a team.
_KNOWN_CROSS_SOURCE_SPELLINGS = [
    # Cape Verde — football-data uses "Cape Verde Islands" (the real bug we hit)
    ("football-data", "Cape Verde Islands", "Cape Verde"),
    ("FIFA / groups CSV", "Cabo Verde", "Cape Verde"),
    ("canonical", "Cape Verde", "Cape Verde"),
    # South Korea
    ("football-data", "Korea Republic", "South Korea"),
    ("martj42", "South Korea", "South Korea"),
    ("alt", "Republic of Korea", "South Korea"),
    # USA
    ("alt", "USA", "United States"),
    ("alt", "United States of America", "United States"),
    ("canonical", "United States", "United States"),
    # Türkiye
    ("legacy", "Turkey", "Türkiye"),
    ("ascii", "Turkiye", "Türkiye"),
    ("canonical", "Türkiye", "Türkiye"),
    # Ivory Coast
    ("alt", "Cote d'Ivoire", "Ivory Coast"),
    ("alt", "Côte d'Ivoire", "Ivory Coast"),
    # Congo DR
    ("alt", "DR Congo", "Congo DR"),
    ("alt", "Democratic Republic of Congo", "Congo DR"),
    # Czechia
    ("legacy", "Czech Republic", "Czechia"),
    # Bosnia
    ("alt", "Bosnia and Herzegovina", "Bosnia-Herzegovina"),
    ("alt", "Bosnia", "Bosnia-Herzegovina"),
    # Curacao
    ("accented", "Curaçao", "Curacao"),
    # Iran (legacy FIFA "IR Iran")
    ("legacy", "IR Iran", "Iran"),
]


def test_known_cross_source_spellings_all_normalize():
    """Every alias football-data / martj42 / api-football could realistically
    return for a WC team MUST resolve to the canonical form used in
    data/wc2026_groups.csv. A failure here = a silent data-join bug."""
    from core.data.teams import normalize
    fails = []
    for source, raw, expected in _KNOWN_CROSS_SOURCE_SPELLINGS:
        got = normalize(raw)
        if got != expected:
            fails.append(f"{source}: '{raw}' -> '{got}' (expected '{expected}')")
    assert not fails, "alias misses:\n  " + "\n  ".join(fails)


def test_all_wc_teams_in_groups_csv_self_canonical():
    """Every team in data/wc2026_groups.csv must be its own canonical form.
    If this fails, the ground-truth file drifted from teams.normalize()."""
    import csv as csv_
    from core.data.teams import normalize
    with open("data/wc2026_groups.csv") as f:
        teams = [r["team"] for r in csv_.DictReader(f)]
    drift = [(t, normalize(t)) for t in teams if normalize(t) != t]
    assert not drift, f"groups CSV teams not self-canonical: {drift}"
    assert len(teams) == 48, f"expected 48 WC teams, got {len(teams)}"


def test_eloratings_code_map_covers_all_wc_teams():
    """The eloratings 2-letter code map MUST cover every WC team. If a team
    is missing, its Elo falls back to the 1500 baseline silently — model bug."""
    import csv as csv_
    from core.data.teams import normalize
    from core.data.eloratings_codes import EL_CODE_TO_TEAM
    with open("data/wc2026_groups.csv") as f:
        truth = {normalize(r["team"]) for r in csv_.DictReader(f)}
    mapped = set(EL_CODE_TO_TEAM.values())
    missing = sorted(truth - mapped)
    assert not missing, f"WC teams missing from eloratings code map: {missing}"


def test_eloratings_code_map_no_duplicate_keys():
    """Python dicts silently keep only the last value for a duplicate key.
    Walk the source AST to catch duplicates that runtime would hide."""
    import ast
    from collections import Counter
    with open("core/data/eloratings_codes.py") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "EL_CODE_TO_TEAM":
            keys = [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
            dups = {k: v for k, v in Counter(keys).items() if v > 1}
            assert not dups, f"duplicate keys in EL_CODE_TO_TEAM: {dups}"
            return
    raise AssertionError("EL_CODE_TO_TEAM annotated assignment not found")


def test_eloratings_code_map_no_duplicate_wc_teams():
    """Two codes mapping to the same WC team would be a data-join ambiguity."""
    import csv as csv_
    from core.data.teams import normalize
    from core.data.eloratings_codes import EL_CODE_TO_TEAM
    with open("data/wc2026_groups.csv") as f:
        truth = {normalize(r["team"]) for r in csv_.DictReader(f)}
    inv: dict[str, list[str]] = {}
    for code, team in EL_CODE_TO_TEAM.items():
        if team in truth:
            inv.setdefault(team, []).append(code)
    multi = {t: cs for t, cs in inv.items() if len(cs) > 1}
    assert not multi, f"WC teams with multiple codes: {multi}"
