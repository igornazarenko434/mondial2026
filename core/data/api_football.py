"""API-Football client — confirmed lineups + injuries source (Day 8).

Free tier: 100 req/day. Wired calls (each ≤ 1 credit):
  - find_fixture_id(home, away, kickoff_utc)  — locate the api-football fixture
                                                  id for one WC 2026 match
  - fetch_lineups(fixture_id)                  — confirmed XI per team
  - fetch_injuries(team_id, season=2026)       — active injuries / suspensions
  - find_team_id(team_name)                    — name → numeric id

Day-9.20 caching strategy:
  - find_team_id: PERMANENT disk cache at store/api_football_team_ids.json
    (team ids never change). One-shot populate via
    tools/populate_api_football_team_ids.py wins all 48 team ids in ~48
    credits; from then on the daemon makes ZERO team-id calls per match.
  - fetch_injuries: 30-MIN in-memory cache. Within a match-window pass
    (T-60m + T-15m for 4 matches), 8 calls collapse to 4 (one per team).
  - find_fixture_id: 12-HOUR in-memory cache. The (home, away, date) pair
    is stable so reusing within a window costs nothing.
  - fetch_lineups: NOT cached — lineups change until kickoff.

Worst-case match day (4 simultaneous matches, group stage final day):
  Without caches: 4 × (1 fixture + 1 lineups + 2 team_id + 2 injuries)
                = 24 per window × 2 windows = 48 per day. Near cap.
  With Day-9.20: 4 × (1 fixture + 1 lineups + 0 team_id + 2 injuries
                       deduped to once per team across the 4 matches)
                = ~10 per window × 2 = 20/day. Comfortable.

Quota-aware skip:
  - `_budget_clear()` short-circuits BEFORE the HTTP if api_football is
    over its 100/day budget. The caller (news_agent.gather_context) then
    falls back to api-football data from CACHE (still present in
    _INJURIES_CACHE) plus Brave search results. No card is ever blocked
    on api-football availability.
"""
from __future__ import annotations
import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any
import requests

from core.data.teams import normalize
from core.obs.logging import get_logger

log = get_logger("data.api_football")

API_BASE = "https://v3.football.api-sports.io"
WC_LEAGUE_ID = 1                # FIFA World Cup competition id on api-football
DEFAULT_SEASON = 2026

# Day-9.20 in-memory + on-disk caches.
_TEAM_ID_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "store", "api_football_team_ids.json")
_TEAM_ID_CACHE: dict[str, int] | None = None
_TEAM_ID_LOCK = threading.RLock()

# {team_id: (timestamp_unix, [injury, ...])}
_INJURIES_CACHE: dict[int, tuple[float, list[dict]]] = {}
INJURIES_TTL_SEC = int(os.environ.get("API_FOOTBALL_INJURIES_TTL", "1800"))  # 30 min

# {(home_canon, away_canon, date_str): (timestamp_unix, fixture_id)}
_FIXTURE_ID_CACHE: dict[tuple[str, str, str], tuple[float, int]] = {}
FIXTURE_ID_TTL_SEC = int(os.environ.get("API_FOOTBALL_FIXTURE_TTL", "43200"))  # 12 h


def _headers() -> dict:
    key = os.environ.get("API_FOOTBALL_KEY")
    if not key:
        raise RuntimeError("Set API_FOOTBALL_KEY in .env")
    return {"x-apisports-key": key}


def _budget_clear() -> bool:
    """Mirror the pattern from oddsapi: short-circuit if the free quota is
    exhausted instead of hitting a hard 429."""
    try:
        from core.obs.cost import ledger
        return not ledger().over_budget("api_football")
    except Exception:
        return True


def _get(endpoint: str, params: dict, *, label: str | None = None) -> dict | None:
    """One GET with budget guard + obs span + graceful error handling."""
    if not _budget_clear():
        log.warning("api_football over budget; skipping GET %s", endpoint)
        return None
    from core import obs
    url = f"{API_BASE}{endpoint}"
    try:
        with obs.external_call("api_football", label or endpoint.lstrip("/"), units=1):
            resp = requests.get(url, headers=_headers(), params=params, timeout=20)
            resp.raise_for_status()
    except Exception as e:                                # noqa: BLE001
        log.warning("api_football GET %s failed: %s", endpoint, e)
        return None
    try:
        return resp.json()
    except ValueError:
        log.warning("api_football GET %s returned non-JSON body", endpoint)
        return None


def _load_team_id_cache() -> dict[str, int]:
    """Day-9.20: load the on-disk team-id cache (populated once via
    tools/populate_api_football_team_ids.py). Returns {} if missing."""
    global _TEAM_ID_CACHE
    with _TEAM_ID_LOCK:
        if _TEAM_ID_CACHE is not None:
            return _TEAM_ID_CACHE
        try:
            with open(_TEAM_ID_CACHE_PATH) as f:
                _TEAM_ID_CACHE = json.load(f).get("teams", {})
        except Exception:                               # noqa: BLE001
            _TEAM_ID_CACHE = {}
        return _TEAM_ID_CACHE


def _save_team_id_cache() -> None:
    """Persist current cache to disk atomically (write-then-rename).

    Uses the shared race-safe atomic-write helper. `_TEAM_ID_LOCK` already
    serializes writers within a single process; the helper additionally
    prevents the `path + ".tmp"` collision pattern that caused the
    Switzerland T-24h failure on 2026-06-17 (see core/data/cache.py).
    """
    global _TEAM_ID_CACHE
    with _TEAM_ID_LOCK:
        if _TEAM_ID_CACHE is None:
            return
        try:
            from core.data.cache import _atomic_write_json
            _atomic_write_json(
                _TEAM_ID_CACHE_PATH,
                {"teams": _TEAM_ID_CACHE,
                 "updated_at": datetime.now(timezone.utc).isoformat()},
                indent=2, ensure_ascii=False,
            )
        except Exception as e:                          # noqa: BLE001
            log.warning("team-id cache save failed: %s", e)


def _cache_team_id(canon_name: str, team_id: int) -> None:
    global _TEAM_ID_CACHE
    with _TEAM_ID_LOCK:
        if _TEAM_ID_CACHE is None:
            _load_team_id_cache()
        _TEAM_ID_CACHE[canon_name] = team_id
        _save_team_id_cache()


def find_fixture_id(home: str, away: str, kickoff_utc: str,
                    league_id: int = WC_LEAGUE_ID,
                    season: int = DEFAULT_SEASON) -> int | None:
    """Locate one fixture's api-football fixture_id.

    Strategy (1 credit max):
      - Query /fixtures?league=<id>&season=<year>&date=<YYYY-MM-DD>
        (this returns ALL WC matches on that date — usually 0-4)
      - Filter results by canonicalised team names. If the home and away match
        on BOTH sides (canonicalised), it's our match.

    Returns the integer fixture_id or None if not found / API down / not yet
    populated (the WC 2026 season is empty on api-football until the founder
    publishes fixtures — we verified earlier and return None gracefully).
    """
    if not kickoff_utc:
        return None
    try:
        date_str = (datetime.fromisoformat(str(kickoff_utc).replace("Z", "+00:00"))
                    .astimezone(timezone.utc).strftime("%Y-%m-%d"))
    except (ValueError, AttributeError):
        log.info("find_fixture_id: cannot parse kickoff_utc=%r", kickoff_utc)
        return None
    h_canon, a_canon = normalize(home), normalize(away)

    # Day-9.20: 12h in-memory cache. Same (home, away, date) within a window
    # pass costs nothing.
    cache_key = (h_canon or "", a_canon or "", date_str)
    now = time.time()
    if cache_key in _FIXTURE_ID_CACHE:
        ts, fid = _FIXTURE_ID_CACHE[cache_key]
        if now - ts < FIXTURE_ID_TTL_SEC:
            return fid

    body = _get("/fixtures",
                {"league": league_id, "season": season, "date": date_str},
                label="fixtures:lookup")
    if not body or not body.get("response"):
        log.info("find_fixture_id: no fixtures on %s for league %s/%s",
                  date_str, league_id, season)
        return None
    for f in body["response"]:
        teams = f.get("teams") or {}
        ah = normalize((teams.get("home") or {}).get("name", "")) or ""
        aa = normalize((teams.get("away") or {}).get("name", "")) or ""
        if ah == h_canon and aa == a_canon:
            fid = int(f.get("fixture", {}).get("id"))
            _FIXTURE_ID_CACHE[cache_key] = (now, fid)
            return fid
    log.info("find_fixture_id: %s vs %s not in api-football's list for %s",
              h_canon, a_canon, date_str)
    return None


def fetch_lineups(fixture_id: int) -> list[dict] | None:
    """GET /fixtures/lineups?fixture=<id>.

    Returns a list (one entry per team):
        [{"team": "Mexico", "formation": "4-3-3",
          "coach": "Aguirre",
          "startXI": ["Ochoa (GK)", "Galindo (DF)", ...],
          "substitutes": [...]}, ...]
    Returns None on failure / empty (lineups not yet published). Confirmed
    XIs typically publish ~1h before kickoff (= our T-60m window).
    """
    if not fixture_id:
        return None
    body = _get("/fixtures/lineups", {"fixture": int(fixture_id)},
                label="fixtures/lineups")
    if not body or not body.get("response"):
        return None
    out = []
    for row in body["response"]:
        team_name = (row.get("team") or {}).get("name", "")
        formation = row.get("formation", "")
        coach = (row.get("coach") or {}).get("name", "")
        start_xi = []
        for p in (row.get("startXI") or []):
            player = (p.get("player") or {})
            start_xi.append(f"{player.get('name', '?')} ({player.get('pos', '?')})")
        subs = []
        for p in (row.get("substitutes") or []):
            player = (p.get("player") or {})
            subs.append(f"{player.get('name', '?')} ({player.get('pos', '?')})")
        out.append({
            "team": normalize(team_name) or team_name,
            "formation": formation,
            "coach": coach,
            "startXI": start_xi,
            "substitutes": subs,
        })
    return out


def fetch_injuries(team_id: int, season: int = DEFAULT_SEASON,
                   league_id: int = WC_LEAGUE_ID,
                   force_refresh: bool = False) -> list[dict] | None:
    """GET /injuries?team=<team_id>&season=<season>&league=<league_id>.

    Day-9.20: in-memory cache with INJURIES_TTL_SEC (default 30 min). Within
    a window pass for 4 simultaneous matches we collapse 8 injury calls
    (2 teams × 4 matches) into ~4 (one per unique team). Across the T-60m +
    T-15m windows for the same match, second call hits the cache.

    When api-football quota is exhausted (`_budget_clear()` returns False)
    we STILL return the cached value if present — gracefully degrading to
    stale data rather than NO data.

    Returns a list:
        [{"player": "Mbappé", "position": "Attacker",
          "type": "Knock", "reason": "Hamstring", "match_date": "..."}]
    """
    if not team_id:
        return None
    now = time.time()
    cached = _INJURIES_CACHE.get(int(team_id))
    if cached and not force_refresh:
        ts, value = cached
        if now - ts < INJURIES_TTL_SEC:
            return value
        # Cache expired but quota out — return stale rather than nothing
        if not _budget_clear():
            log.warning("api_football over budget; serving STALE injuries for "
                        "team_id=%d (age %.0fs)", team_id, now - ts)
            return value

    body = _get("/injuries",
                {"team": int(team_id), "season": int(season), "league": int(league_id)},
                label="injuries")
    if not body or not body.get("response"):
        # Cache the empty result too — saves repeating the call for the
        # same team within the window
        _INJURIES_CACHE[int(team_id)] = (now, [])
        return []
    out = []
    for row in body["response"]:
        player = row.get("player") or {}
        meta = row.get("player") or {}  # api-football puts both type and reason here
        # The injury status itself is on row["player"] or row["fixture"] depending
        # on the api version; we surface whatever is present.
        out.append({
            "player": player.get("name", "?"),
            "position": player.get("position", "?"),
            "type": meta.get("type", ""),
            "reason": meta.get("reason", ""),
            "match_date": (row.get("fixture") or {}).get("date", ""),
        })
    _INJURIES_CACHE[int(team_id)] = (now, out)
    return out


# Day-9.20: alternate api-football naming conventions per canonical team.
# Order: most-likely first. We try each variant against api-football until
# one returns a match. Caches the winning team_id so we never re-do this.
_TEAM_NAME_VARIANTS: dict[str, list[str]] = {
    "Czechia": ["Czechia", "Czech Republic"],
    "Bosnia-Herzegovina": ["Bosnia", "Bosnia and Herzegovina",
                            "Bosnia & Herzegovina", "Bosnia-Herzegovina"],
    "South Korea": ["Korea Republic", "South Korea", "Republic of Korea"],
    "Türkiye": ["Türkiye", "Turkey"],
    "United States": ["USA", "United States", "United States of America"],
    "Ivory Coast": ["Ivory Coast", "Côte d'Ivoire", "Cote d'Ivoire"],
    "Congo DR": ["DR Congo", "Congo DR", "Democratic Republic of Congo"],
    "Cape Verde": ["Cape Verde Islands", "Cape Verde", "Cabo Verde"],
    "Iran": ["Iran", "IR Iran"],
    "Curacao": ["Curaçao", "Curacao"],
}


def find_team_id(team_name: str, league_id: int = WC_LEAGUE_ID,
                 season: int = DEFAULT_SEASON) -> int | None:
    """Resolve our canonical team name → api-football's numeric team_id.

    Day-9.20:
      1. Hit the PERMANENT disk cache (store/api_football_team_ids.json)
         first — if present, return without burning quota.
      2. Otherwise try each api-football naming variant from
         _TEAM_NAME_VARIANTS (handles "Czechia" ↔ "Czech Republic",
         "Bosnia-Herzegovina" ↔ "Bosnia" / "Bosnia and Herzegovina", etc.)
      3. Cache the winning team_id to disk so future calls cost 0.

    Run `tools/populate_api_football_team_ids.py` once after quota resets
    to pre-warm all 48 WC2026 team ids (~48 credits one-shot). After that
    the daemon makes zero team-id calls per card-window.
    """
    canon = normalize(team_name) or team_name
    cache = _load_team_id_cache()
    if canon in cache:
        return int(cache[canon])

    # Build the search candidate list: known variants then plain canonical
    variants = list(_TEAM_NAME_VARIANTS.get(canon, []))
    if canon not in variants:
        variants.insert(0, canon)

    for variant in variants:
        body = _get("/teams",
                    {"league": league_id, "season": season, "search": variant},
                    label="teams:search")
        if not body or not body.get("response"):
            # Fall back to unfiltered search
            body = _get("/teams", {"search": variant},
                        label="teams:search_global")
        if not body or not body.get("response"):
            continue
        for row in body["response"]:
            team = row.get("team") or {}
            if normalize(team.get("name", "")) == canon:
                tid = int(team.get("id"))
                _cache_team_id(canon, tid)
                return tid
        # First-result heuristic — same as before but only on first variant
        if variant == variants[0]:
            first = body["response"][0].get("team") or {}
            if first.get("id"):
                tid = int(first["id"])
                _cache_team_id(canon, tid)
                return tid
    return None


# ---- Day-4-spec fallback (kept for reliability layer) ----

def fixtures_backup() -> list[dict]:
    """Fallback fixtures source when football-data.org is down. Returns the
    same shape as football_data.fetch_wc_matches(). Day-4 leftover; not
    consumed by Day 8 but kept here for the reliability fallback."""
    body = _get("/fixtures", {"league": WC_LEAGUE_ID, "season": DEFAULT_SEASON},
                label="fixtures:all")
    if not body or not body.get("response"):
        return []
    out = []
    for f in body["response"]:
        fix = f.get("fixture") or {}
        teams = f.get("teams") or {}
        score = (f.get("score") or {}).get("fulltime") or {}
        out.append({
            "match_id": int(fix.get("id", 0)),
            "utc_kickoff": fix.get("date", ""),
            "local_kickoff": fix.get("date", ""),
            "stage": (f.get("league") or {}).get("round", ""),
            "group": None,
            "home": normalize((teams.get("home") or {}).get("name", "")),
            "away": normalize((teams.get("away") or {}).get("name", "")),
            "status": (fix.get("status") or {}).get("short", ""),
            "home_goals": score.get("home"),
            "away_goals": score.get("away"),
        })
    return out
