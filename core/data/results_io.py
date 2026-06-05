"""Historical international results — the training data for the Dixon-Coles fit
(Day 3). Rows are normalized to {home, away, home_goals, away_goals, days_ago}.

The `fetch` function is injectable so the shaping/normalization is tested offline;
the live download (a free international-results dataset or soccerdata) runs on your
machine. Use only national-team 'A' matches from roughly the last ~4 years.
"""
from __future__ import annotations
from datetime import date
from core.data.teams import normalize


def historical_results(fetch=None) -> list[dict]:
    """Return normalized result rows. `fetch()` yields dict rows with at least
    home, away, home_goals, away_goals, and either days_ago or date (ISO)."""
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


def _fetch_live():
    """LIVE (your machine): download national-team 'A' results (~last 4y). Options:
    a free 'international football results' CSV dataset, or soccerdata. Yield rows
    with home/away/home_goals/away_goals/date. Raises until wired so a bad source
    can't silently train the model on garbage."""
    raise NotImplementedError(
        "Wire a historical international-results source (or pass fetch=...). "
        "Normalization + shaping here are already implemented & tested.")
