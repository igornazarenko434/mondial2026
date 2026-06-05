"""Historical international results — the training data for the Dixon-Coles fit
(Day 3). Rows are normalized to {home, away, home_goals, away_goals, days_ago}.

The `fetch` function is injectable so the shaping/normalization is tested offline;
the live download (martj42/international_results on GitHub) runs on your machine
and is disk-cached (24h) so a daily refit doesn't re-fetch 3.7MB every call.
Use only national-team 'A' matches from roughly the last ~4 years.
"""
from __future__ import annotations
import os
from datetime import date
from core.data.teams import normalize
from core.data.cache import cached_json

CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "store")
_DEFAULT = object()


def historical_results(fetch=None, cache_path=_DEFAULT, ttl_hours: float = 24) -> list[dict]:
    """Return normalized result rows. `fetch()` yields dict rows with at least
    home, away, home_goals, away_goals, and either days_ago or date (ISO).

    Disk-cached via cached_json: re-fits hit local disk for 24h instead of
    re-downloading martj42's 3.7MB CSV. cache_path=None disables caching
    (e.g. inside tests where `fetch` already provides the data)."""
    def produce():
        rows = (fetch or _fetch_live)()
        out = []
        today = date.today()
        for r in rows:
            h, a = normalize(r.get("home")), normalize(r.get("away"))
            if not h or not a or r.get("home_goals") is None or r.get("away_goals") is None:
                continue
            if "days_ago" in r and r["days_ago"] is not None:
                days = int(r["days_ago"])
            elif r.get("date"):
                days = (today - date.fromisoformat(str(r["date"])[:10])).days
            else:
                days = 0
            out.append({"home": h, "away": a,
                        "home_goals": int(r["home_goals"]), "away_goals": int(r["away_goals"]),
                        "days_ago": max(0, days)})
        return out
    # Skip caching when caller supplied their own fetch (tests, custom sources).
    if fetch is not None:
        return produce()
    path = (os.path.join(CACHE_DIR, "results_history.json")
            if cache_path is _DEFAULT else cache_path)
    return cached_json(path, ttl_hours, produce)


MARTJ42_RESULTS_URL = (
    "https://raw.githubusercontent.com/martj42/international_results/master/results.csv"
)
HISTORY_WINDOW_YEARS = 4   # only fit on the last ~4y for tactical relevance


def _fetch_live(url: str = MARTJ42_RESULTS_URL, http_get=None,
                window_years: int = HISTORY_WINDOW_YEARS):
    """LIVE: download national-team A-match results from the maintained
    `martj42/international_results` GitHub dataset (CSV, ~49k rows
    1872->today, updated within hours of every fixture).

    Returns rows shaped {home, away, home_goals, away_goals, date}, filtered
    to the last `window_years` years and skipping rows without final scores
    (scheduled future fixtures appear as home_score/away_score = 'NA').

    http_get is injectable so tests can mock without hitting the network.
    """
    import requests, csv, io
    from datetime import date, timedelta
    if http_get is None:
        from core import obs
        def _do():
            with obs.external_call("martj42", "results_csv"):
                r = requests.get(url, timeout=30)
                r.raise_for_status()
                return r.text
        text = _do()
    else:
        text = http_get(url)
    cutoff = date.today() - timedelta(days=365 * window_years)
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        hg, ag = row.get("home_score"), row.get("away_score")
        if not hg or not ag or hg == "NA" or ag == "NA":
            continue                              # future or void match
        try:
            d = date.fromisoformat(row["date"])
        except (KeyError, ValueError):
            continue
        if d < cutoff:
            continue
        out.append({
            "home": row.get("home_team", ""),
            "away": row.get("away_team", ""),
            "home_goals": int(hg),
            "away_goals": int(ag),
            "date": row["date"],
        })
    return out
