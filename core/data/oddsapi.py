"""Odds client + de-vig + Day-4 event→fixture matching + snapshot persistence.

The locked odds at T-7m are not just data — they are the SCORING MULTIPLIER
under the Toto rules (`points = base × odds`). The pipeline pulls odds at
T-60m / T-15m / T-7m, snapshots each window into `odds_snapshots`, and the
T-7m snapshot is what every `score_match()` resolves against post-game.

`devig` is fully working (multiplicative normalization -> fair probabilities).
"""
from __future__ import annotations
import os
import sqlite3
from datetime import datetime
import requests
from core.data.teams import normalize
from core.obs.logging import get_logger

log = get_logger("data.oddsapi")

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
# Fallback default; the exact key is RESOLVED DYNAMICALLY from /sports (the
# 2026 World Cup key isn't guaranteed by name, so don't hard-code it).
SOCCER_KEY_DEFAULT = "soccer_fifa_world_cup"

# Bookmaker preference order: Pinnacle is the sharpest market (lowest margin,
# fast price corrections), Betfair Exchange next; if neither is present we
# fall back to a synthetic consensus = de-vigged average of every book.
DEFAULT_PREFER_BOOKS = ("pinnacle", "betfair_ex_eu", "betfair_ex_uk", "betfair")


def list_sports() -> list[dict]:
    """All active sports/keys from The Odds API (the /sports call is FREE
    — does NOT count against the 500/mo quota; per the-odds-api.com docs)."""
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        raise RuntimeError("Set ODDS_API_KEY in .env")
    # units=0: the call is metered for trace/rate-limit only; no credit burn.
    from core import obs
    with obs.external_call("odds_api", "sports", units=0):
        resp = requests.get(f"{ODDS_API_BASE}/sports",
                             params={"apiKey": key}, timeout=20)
        resp.raise_for_status()
        return resp.json()


def resolve_wc_key() -> str:
    """Find the live World Cup sport key (e.g. 'soccer_fifa_world_cup');
    robust to renaming, falls back to the default if /sports can't be reached."""
    try:
        for s in list_sports():
            key = s.get("key", "")
            title = (s.get("title", "") + s.get("group", "")).lower()
            if "world_cup" in key or "world cup" in title:
                if ("women" not in key and "women" not in title
                        and "winner" not in key):
                    return s["key"]
    except Exception as e:
        log.warning("resolve_wc_key failed (%s); falling back to default", e)
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
    """Average de-vigged probabilities across several books (Pinnacle/Betfair…)."""
    probs = [devig(o) for o in book_odds]
    return {k: sum(p[k] for p in probs) / len(probs) for k in ("H", "D", "A")}


# ───────────────────────── Day-4 event matching ──────────────────────────

def _to_hda(event: dict, market: dict) -> dict | None:
    """Convert an h2h market's outcomes to {H, D, A} keyed by event teams.
    Returns None if the market isn't h2h or any side is missing."""
    if not market or market.get("key") != "h2h":
        return None
    by_name = {o.get("name"): o.get("price") for o in market.get("outcomes", [])}
    home_team, away_team = event.get("home_team"), event.get("away_team")
    out = {"H": by_name.get(home_team), "D": by_name.get("Draw"),
           "A": by_name.get(away_team)}
    return out if all(out.values()) else None


def match_event_to_fixture(events: list[dict], home: str, away: str,
                           kickoff_utc: str | None = None,
                           window_hours: float = 36.0) -> dict | None:
    """Pure: find the API event corresponding to one specific WC fixture.

    Matches by NORMALIZED team names on both sides (so 'Korea Republic' joins
    correctly with our canonical 'South Korea'). If kickoff_utc is given, also
    requires the event's commence_time to be within +/- window_hours — this
    prevents accidental matches against a friendly months later between the
    same teams.
    """
    h_canon, a_canon = normalize(home), normalize(away)
    target_dt = None
    if kickoff_utc:
        try:
            target_dt = datetime.fromisoformat(str(kickoff_utc).replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            target_dt = None    # bad fmt → don't apply date filter
    for ev in events or []:
        eh = normalize(ev.get("home_team", ""))
        ea = normalize(ev.get("away_team", ""))
        if eh != h_canon or ea != a_canon:
            continue
        if target_dt is not None:
            ct = ev.get("commence_time")
            try:
                ev_dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            if abs((ev_dt - target_dt).total_seconds()) > window_hours * 3600:
                continue
        return ev
    return None


def pick_book(event: dict,
              prefer: tuple[str, ...] = DEFAULT_PREFER_BOOKS
              ) -> tuple[str, dict] | None:
    """Pure: pick the best available bookmaker on this event.

    Iterates prefer in order (sharpest-first); returns (book_key, {H,D,A})
    on the first hit. Returns None if no preferred book is present.
    """
    if not event:
        return None
    by_key = {b.get("key"): b for b in event.get("bookmakers", [])}
    for k in prefer:
        b = by_key.get(k)
        if not b:
            continue
        for m in b.get("markets", []):
            odds = _to_hda(event, m)
            if odds:
                return k, odds
    return None


def consensus_book(event: dict) -> tuple[str, dict] | None:
    """Pure: synthetic 'consensus' book = de-vigged average across all books,
    converted back to decimal odds. Last-resort fallback for fixtures where
    none of our preferred books are listed.
    """
    odds_lists: list[dict] = []
    for b in event.get("bookmakers", []) or []:
        for m in b.get("markets", []):
            odds = _to_hda(event, m)
            if odds:
                odds_lists.append(odds)
                break    # one market per book is enough
    if not odds_lists:
        return None
    probs = consensus_probs(odds_lists)    # devigged probs sum to 1
    return "consensus", {k: round(1.0 / probs[k], 3) for k in ("H", "D", "A")}


# ──────────────────────── Day-4 HTTP + persistence ───────────────────────

def fetch_all_odds(regions: str = "eu,uk", markets: str = "h2h") -> list[dict]:
    """One HTTP call returns ALL events for the WC. Credits used = markets x
    regions per call (free tier 500/mo). Caller batches matches against this
    list to stay within budget — never call this once per match.
    """
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        raise RuntimeError("Set ODDS_API_KEY in .env")
    from core import obs
    sport_key = resolve_wc_key()
    url = f"{ODDS_API_BASE}/sports/{sport_key}/odds"
    params = {"apiKey": key, "regions": regions, "markets": markets,
              "oddsFormat": "decimal"}
    units = len(markets.split(",")) * len(regions.split(","))
    with obs.external_call("odds_api", "odds", units=units):
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
    return resp.json()


def fetch_match_odds(home: str, away: str,
                     kickoff_utc: str | None = None,
                     regions: str = "eu,uk", markets: str = "h2h",
                     prefer_books: tuple[str, ...] = DEFAULT_PREFER_BOOKS,
                     events: list[dict] | None = None) -> dict | None:
    """Pull current 1X2 odds for a single WC fixture. Returns
    {'H','D','A','book'} (decimal odds) or None if no match / no usable odds.

    Budget-guarded: returns None and logs a warning if the odds_api budget is
    exhausted (so the pipeline degrades to model-only instead of hitting a
    hard 429).

    Batch use: pass events= (e.g. from `fetch_all_odds()`) to share one HTTP
    call across many fixtures. Per kickoff window, fetch once → match many.
    """
    # Budget guard — never burn the free quota on a call we already know
    # would put us over (caller can still fall back to model-only).
    try:
        from core.obs.cost import ledger
        if ledger().over_budget("odds_api"):
            log.warning("odds_api over budget; returning None "
                        "(pipeline degrades to model-only)")
            return None
    except Exception:
        pass     # obs not available; proceed

    if events is None:
        events = fetch_all_odds(regions=regions, markets=markets)
    ev = match_event_to_fixture(events, home, away, kickoff_utc=kickoff_utc)
    if not ev:
        return None
    pick = pick_book(ev, prefer=prefer_books) or consensus_book(ev)
    if not pick:
        return None
    book, odds = pick
    return {**odds, "book": book}


def snapshot_odds(conn: sqlite3.Connection, match_id: int, captured_at: str,
                  book: str, odds: dict) -> None:
    """Persist one odds snapshot row.

    captured_at: window label — 'T-24h' / 'T-60m' / 'T-15m' / 'T-7m'.
    book: 'pinnacle' / 'betfair_ex_eu' / 'consensus' / etc.
    Upsert on (match_id, captured_at, book) — re-running a window updates
    cleanly instead of duplicating.
    """
    conn.execute(
        "INSERT INTO odds_snapshots (match_id, captured_at, book, "
        "odds_h, odds_d, odds_a) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(match_id, captured_at, book) DO UPDATE SET "
        "odds_h=excluded.odds_h, odds_d=excluded.odds_d, odds_a=excluded.odds_a",
        (match_id, captured_at, book, odds.get("H"), odds.get("D"), odds.get("A")))
    conn.commit()


# Window-preference list — the T-7m label is the LOCKED scoring multiplier
# under the Toto rules; we fall back to earlier windows only if the lock
# snapshot is unavailable for some reason.
WINDOW_PREFERENCE = ("T-7m", "T-15m", "T-60m", "T-24h", "T-pre-tourney")


def latest_snapshot(conn: sqlite3.Connection, match_id: int,
                    prefer_windows: tuple[str, ...] = WINDOW_PREFERENCE,
                    prefer_books: tuple[str, ...] = DEFAULT_PREFER_BOOKS
                    ) -> dict | None:
    """Return the best available snapshot for a match.

    Resolution rules (in this order):
      1. Walk prefer_windows in order — first window with any rows wins.
      2. Within that window, walk prefer_books in order — sharpest book wins.
      3. If no preferred window has rows, fall back to ANY snapshot (sorted
         by book preference) so a manually-inserted row still scores.

    Used by `core.scoring.standings_writer.score_one_match` to look up the
    LOCKED scoring multiplier — `T-7m` lock by default; explicit override
    via `prefer_windows=("T-7m",)` to force a strict lock-only lookup.
    """
    book_idx = {b: i for i, b in enumerate(prefer_books + ("consensus",))}

    def _best_row(rows):
        rows.sort(key=lambda r: book_idx.get(r[1], 99))
        captured_at, book, h, d, a = rows[0]
        return {"captured_at": captured_at, "book": book,
                "H": h, "D": d, "A": a}

    for window in prefer_windows:
        rows = conn.execute(
            "SELECT captured_at, book, odds_h, odds_d, odds_a "
            "FROM odds_snapshots WHERE match_id=? AND captured_at=?",
            (match_id, window)).fetchall()
        if rows:
            return _best_row(rows)
    # No preferred-window snapshot — fall back to any row by book preference.
    rows = conn.execute(
        "SELECT captured_at, book, odds_h, odds_d, odds_a "
        "FROM odds_snapshots WHERE match_id=?", (match_id,)).fetchall()
    return _best_row(rows) if rows else None
