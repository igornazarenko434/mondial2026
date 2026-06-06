"""Day-7: futures-market odds fetchers.

the-odds-api exposes WC futures markets as separate "sport keys":
  - `soccer_fifa_world_cup_winner`        — outright winner
  - `soccer_fifa_world_cup_topscorer`     — top scorer  (NOT always present
                                              on free tier; we probe and
                                              gracefully return None)

Each call is 1 credit (1 market × 1 region). Both calls + a `list_sports`
probe come in at <5 credits — comfortable within the free 500/mo budget.

Team / player names returned by the-odds-api are run through
`core.data.teams.normalize` (for teams) so they line up with our
`WINNER_PAYOUT` keys. Player names get a lighter cleanup since they have no
canonical alias table yet — exact-match lookup against `SCORER_PAYOUT`.
"""
from __future__ import annotations
import os
import requests
from core.data.teams import normalize
from core.obs.logging import get_logger

log = get_logger("data.futures_odds")

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
WINNER_KEY_DEFAULT = "soccer_fifa_world_cup_winner"
TOPSCORER_CANDIDATE_SUFFIXES = (
    "_topscorer", "_top_scorer", "_top_goal_scorer",
    "_goldenboot", "_golden_boot", "_topgoalscorer",
)


def _api_key() -> str:
    k = os.environ.get("ODDS_API_KEY")
    if not k:
        raise RuntimeError("Set ODDS_API_KEY in .env")
    return k


def _budget_clear() -> bool:
    """Same pattern as fetch_match_odds — short-circuit if free tier exhausted."""
    try:
        from core.obs.cost import ledger
        return not ledger().over_budget("odds_api")
    except Exception:
        return True


def _resolve_topscorer_key() -> str | None:
    """Search /sports for a WC top-scorer market. Returns the key if present,
    None otherwise. /sports is FREE (not charged against credits)."""
    from core.data.oddsapi import list_sports
    try:
        sports = list_sports()
    except Exception as e:
        log.warning("resolve_topscorer_key: list_sports failed: %s", e)
        return None
    for s in sports:
        key = (s.get("key") or "").lower()
        if "world_cup" in key and any(suf in key for suf in TOPSCORER_CANDIDATE_SUFFIXES):
            if "women" not in key:
                return s["key"]
    return None


def _fetch_outrights(sport_key: str, regions: str = "eu,uk",
                    markets: str = "outrights") -> list[dict]:
    """One outright-market call. credits = markets × regions. Caller is
    expected to budget-guard BEFORE invoking this (see fetch_*_outright)."""
    from core import obs
    units = len(markets.split(",")) * len(regions.split(","))
    url = f"{ODDS_API_BASE}/sports/{sport_key}/odds"
    params = {"apiKey": _api_key(), "regions": regions, "markets": markets,
              "oddsFormat": "decimal"}
    with obs.external_call("odds_api", f"outrights:{sport_key}", units=units):
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
    return resp.json()


def _best_outright_per_runner(events: list[dict]) -> dict[str, float]:
    """Across all books in the event response, pick the SHORTEST (sharpest)
    decimal price per runner. Outright markets are 'many runners' (e.g. 32
    teams), each with a single H-style 'name' + 'price'."""
    best: dict[str, float] = {}
    for ev in events or []:
        for bm in ev.get("bookmakers") or []:
            for m in bm.get("markets") or []:
                # outright market structure: outcomes = [{name, price}, ...]
                for o in m.get("outcomes") or []:
                    name = o.get("name")
                    price = o.get("price")
                    if not name or not isinstance(price, (int, float)) or price <= 1.0:
                        continue
                    if name not in best or price < best[name]:
                        best[name] = float(price)
    return best


def fetch_winner_outright(regions: str = "eu,uk") -> dict[str, float] | None:
    """Pull the WC tournament-winner outright market. Returns a {canonical
    team name: best decimal odds} dict, or None on failure / over-budget.
    Costs roughly 2 credits (markets × regions on the free tier).

    Names normalized via teams.normalize so the result lines up with
    config.rules.WINNER_PAYOUT keys.
    """
    if not _budget_clear():
        log.warning("odds_api over budget; skipping winner outright fetch")
        return None
    try:
        events = _fetch_outrights(WINNER_KEY_DEFAULT, regions=regions)
    except Exception as e:
        log.warning("fetch_winner_outright failed: %s", e)
        return None
    raw = _best_outright_per_runner(events)
    if not raw:
        return None
    out: dict[str, float] = {}
    skipped: list[str] = []
    for name, price in raw.items():
        canon = normalize(name)
        if canon:
            # Keep the SHORTEST odds across name variants that canonicalize together
            if canon not in out or price < out[canon]:
                out[canon] = price
        else:
            skipped.append(name)
    if skipped:
        log.info("winner outright: %d uncanonicalizable names skipped: %s",
                 len(skipped), skipped[:5])
    return out


def fetch_topscorer_outright(regions: str = "eu,uk") -> dict[str, float] | None:
    """Pull a WC top-scorer outright market. Many free-tier accounts lack this
    market — in which case we return None and the lock script falls back to
    the MC-based expected-team-goals proxy.

    Returns {raw_player_name: best_decimal_odds}. Player names are NOT
    canonicalized (no alias table exists); the caller compares against
    config.rules.SCORER_PAYOUT keys with a fuzzy-match helper.
    """
    sport_key = _resolve_topscorer_key()
    if not sport_key:
        log.info("topscorer market not listed by the-odds-api — skipping")
        return None
    if not _budget_clear():
        log.warning("odds_api over budget; skipping topscorer outright fetch")
        return None
    try:
        events = _fetch_outrights(sport_key, regions=regions)
    except Exception as e:
        log.warning("fetch_topscorer_outright failed: %s", e)
        return None
    return _best_outright_per_runner(events) or None
