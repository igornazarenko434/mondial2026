"""Day-2 data agent: national-team Elo (eloratings.net) + team stats/xG (FBref via
`soccerdata`). All names are normalized (core.data.teams) and results are cached
daily (core.data.cache). The fetch/read functions are injectable so the shaping,
normalization, caching and lookup logic is unit-tested offline; the live network
fetchers are thin and run on your machine with the libs installed.

Outputs feed the model:
  national_team_elo() -> {canonical_team: elo}            → elo.outcome_probs / expected_goals
  fbref_team_stats()  -> {canonical_team: {xg_for, xg_against, matches}}  → enrichment / DC prior
"""
from __future__ import annotations
import os
from core.data.teams import normalize
from core.data.cache import cached_json
from core.obs.logging import get_logger

log = get_logger("data.soccerdata")
DEFAULT_ELO = 1500.0
CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "store")
_DEFAULT = object()   # sentinel: distinguishes "not given" (use default path) from None (no cache)


# ---------------- National-team Elo ----------------
def national_team_elo(fetch=None, cache_path=_DEFAULT, ttl_hours: float = 24) -> dict:
    """{canonical_team: elo_rating}. `fetch()` returns an iterable of (name, elo).
    cache_path: omit for the default daily cache; pass None to disable caching."""
    def produce():
        rows = (fetch or _fetch_eloratings)()
        out = {}
        for name, elo in rows:
            cn = normalize(name)
            if cn:
                try:
                    out[cn] = float(elo)
                except (TypeError, ValueError):
                    log.warning("bad elo for %s: %r", name, elo)
        return out
    path = os.path.join(CACHE_DIR, "elo.json") if cache_path is _DEFAULT else cache_path
    return cached_json(path, ttl_hours, produce)


ELORATINGS_TSV_URL = "https://www.eloratings.net/World.tsv"


def _fetch_eloratings(url: str = ELORATINGS_TSV_URL, http_get=None):
    """LIVE: download eloratings.net/World.tsv and yield (team_name, elo) rows.

    The TSV is tab-separated and has no header. Column layout (verified
    Jun 2026): row_number, rank, team_code, current_elo, +stats... We only
    need columns [2] (eloratings 2-letter code) and [3] (current Elo rating).
    The code map (core.data.eloratings_codes) translates the football-specific
    eloratings codes to canonical team names; unknown codes are skipped (logged
    once per call) so the team falls back to the neutral 1500 baseline in elo_of().

    http_get is injectable so tests can mock without hitting the network.
    """
    import requests
    from core.data.eloratings_codes import EL_CODE_TO_TEAM
    if http_get is None:
        from core import obs
        def _do():
            with obs.external_call("eloratings", "world_tsv"):
                r = requests.get(url, timeout=20)
                r.raise_for_status()
                return r.text
        text = _do()
    else:
        text = http_get(url)
    out, unknown = [], set()
    for line in text.strip().split("\n"):
        cols = line.split("\t")
        if len(cols) < 4:
            continue
        code, elo_raw = cols[2].strip(), cols[3].strip()
        team = EL_CODE_TO_TEAM.get(code)
        if not team:
            unknown.add(code)
            continue
        try:
            out.append((team, float(elo_raw)))
        except ValueError:
            log.warning("eloratings: bad rating %r for code %s", elo_raw, code)
    if unknown:
        log.info("eloratings: %d codes not in map (skipped, fall back to 1500): %s",
                 len(unknown), sorted(unknown)[:10])
    return out


def elo_of(elo: dict, team: str, default: float = DEFAULT_ELO) -> float:
    """Elo for a team (normalized); falls back to a neutral baseline if unknown so
    a missing team never crashes the model."""
    return float(elo.get(normalize(team), default))


def match_elos(elo: dict, home: str, away: str) -> tuple[float, float]:
    return elo_of(elo, home), elo_of(elo, away)


# ---------------- FBref team stats / xG ----------------
def fbref_team_stats(season: str = "2025-2026", read=None,
                     cache_path=_DEFAULT, ttl_hours: float = 24) -> dict:
    """{canonical_team: {xg_for, xg_against, matches}}. `read()` returns an iterable
    of dict rows with keys team / xg_for / xg_against / matches."""
    def produce():
        rows = (read or (lambda: _read_fbref(season)))()
        out = {}
        for r in rows:
            t = normalize(r.get("team"))
            if not t:
                continue
            out[t] = {"xg_for": float(r.get("xg_for") or 0.0),
                      "xg_against": float(r.get("xg_against") or 0.0),
                      "matches": int(r.get("matches") or 0)}
        return out
    path = (os.path.join(CACHE_DIR, f"fbref_{season}.json")
            if cache_path is _DEFAULT else cache_path)
    return cached_json(path, ttl_hours, produce)


def _read_fbref(season: str):
    """LIVE (run on your machine): pull team xG via soccerdata.FBref and yield
    dict rows. soccerdata caches locally; respect its rate limits."""
    try:
        import soccerdata as sd  # noqa: F401
    except ImportError as e:
        raise NotImplementedError("pip install soccerdata; then read FBref team xG") from e
    raise NotImplementedError(
        "Wire soccerdata.FBref(...).read_team_season_stats() → rows of "
        "{team, xg_for, xg_against, matches}. Normalization/caching already handled.")
