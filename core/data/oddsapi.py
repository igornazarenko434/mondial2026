"""Odds client + de-vig.

`devig` is fully working (multiplicative normalization -> fair probabilities).
`fetch_match_odds` calls The Odds API (free tier 500 req/mo); only invoke it
inside the active match window so you stay under quota.
"""
from __future__ import annotations
import os
import requests

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
# Fallback default; the exact key is RESOLVED DYNAMICALLY from /sports (verified
# June 2026: the World Cup key isn't guaranteed by name, so don't hard-code it).
SOCCER_KEY_DEFAULT = "soccer_fifa_world_cup"


def list_sports() -> list[dict]:
    """All active sports/keys from The Odds API (the /sports call is FREE)."""
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        raise RuntimeError("Set ODDS_API_KEY in .env")
    resp = requests.get(f"{ODDS_API_BASE}/sports", params={"apiKey": key}, timeout=20)
    resp.raise_for_status()
    return resp.json()


def resolve_wc_key() -> str:
    """Find the live World Cup sport key (e.g. 'soccer_fifa_world_cup'); robust to
    renaming. Falls back to the default if /sports can't be reached."""
    try:
        for s in list_sports():
            key, title = s.get("key", ""), (s.get("title", "") + s.get("group", "")).lower()
            if "world_cup" in key or "world cup" in title:
                if "women" not in key and "women" not in title:
                    return s["key"]
    except Exception:
        pass
    return SOCCER_KEY_DEFAULT


def devig(odds: dict) -> dict:
    """Decimal odds {'H','D','A'} -> fair (no-vig) probabilities summing to 1.

    Robust to missing/zero/negative odds: only valid (>1.0) outcomes are used.
    Raises ValueError if fewer than 2 valid outcomes (caller should fall back to
    a model-only pick rather than crash).
    """
    valid = {k: v for k, v in (odds or {}).items()
             if isinstance(v, (int, float)) and v and v > 1.0}
    if len(valid) < 2:
        raise ValueError(f"need >=2 valid decimal odds, got {odds}")
    implied = {k: 1.0 / v for k, v in valid.items()}
    total = sum(implied.values())          # > 1 by the bookmaker margin
    return {k: v / total for k, v in implied.items()}


def consensus_probs(book_odds: list[dict]) -> dict:
    """Average de-vigged probabilities across several books (Pinnacle/Betfair...)."""
    probs = [devig(o) for o in book_odds]
    return {k: sum(p[k] for p in probs) / len(probs) for k in ("H", "D", "A")}


def fetch_match_odds(home: str, away: str, regions: str = "eu",
                     markets: str = "h2h") -> dict | None:
    """Pull current 1X2 odds for a fixture. Returns {'H','D','A'} decimal odds.

    TODO(day4): match the API event to your fixture by team names + date, and
    prefer Pinnacle/Betfair when present (sharpest). Snapshot the result at
    T-7m as the LOCKED odds (your scoring multiplier).
    """
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        raise RuntimeError("Set ODDS_API_KEY in .env")
    from core import obs
    sport_key = resolve_wc_key()           # dynamic, not hard-coded
    url = f"{ODDS_API_BASE}/sports/{sport_key}/odds"
    params = {"apiKey": key, "regions": regions, "markets": markets,
              "oddsFormat": "decimal"}
    # 1 call returns ALL events; cost = #markets x #regions credits (free tier 500/mo)
    units = len(markets.split(",")) * len(regions.split(","))
    with obs.external_call("odds_api", "odds", units=units):
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
    for event in resp.json():
        if home in event.get("home_team", "") and away in event.get("away_team", ""):
            for bm in event.get("bookmakers", []):
                outcomes = bm["markets"][0]["outcomes"]
                o = {x["name"]: x["price"] for x in outcomes}
                return {"H": o.get(event["home_team"]),
                        "D": o.get("Draw"),
                        "A": o.get(event["away_team"]),
                        "book": bm["key"]}
    return None
