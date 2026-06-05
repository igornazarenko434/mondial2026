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


def _fetch_eloratings():
    """LIVE (run on your machine): scrape eloratings.net/2026 → [(team, elo), ...].
    Verified Jun 2026: active, updated after each fixture, highest predictive power.
    Fallback source if the layout changes: international-football.net/elo-ratings-table.
    Raises until wired so a half-working scrape can't feed silent garbage."""
    raise NotImplementedError(
        "Wire the eloratings.net scrape (or pass fetch=...). The surrounding "
        "normalize/cache/lookup logic is already implemented & tested.")


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
