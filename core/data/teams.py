"""Team-name normalization across data sources.

football-data.org, FBref, eloratings and the rules file spell some countries
differently ("Korea Republic" vs "South Korea", "Cabo Verde" vs "Cape Verde",
"Türkiye" vs "Turkey", "DR Congo" vs "Congo DR"). Joining stats/Elo/odds to a
fixture silently fails on these mismatches, so EVERY cross-source lookup must go
through `normalize()`. Canonical names match `data/wc2026_groups.csv`.
"""
from __future__ import annotations
import unicodedata

# alias (any source spelling) -> canonical
_ALIASES = {
    "korea republic": "South Korea", "republic of korea": "South Korea",
    "south korea": "South Korea",
    "usa": "United States", "united states of america": "United States",
    "united states": "United States",
    "turkiye": "Türkiye", "turkey": "Türkiye", "türkiye": "Türkiye",
    "cote d'ivoire": "Ivory Coast", "côte d'ivoire": "Ivory Coast",
    "ivory coast": "Ivory Coast",
    "dr congo": "Congo DR", "democratic republic of congo": "Congo DR",
    "congo dr": "Congo DR", "congo": "Congo DR",
    "cabo verde": "Cape Verde", "cape verde": "Cape Verde",
    "cape verde islands": "Cape Verde",   # football-data.org's spelling
    "czech republic": "Czechia", "czechia": "Czechia",
    "bosnia and herzegovina": "Bosnia-Herzegovina",
    "bosnia & herzegovina": "Bosnia-Herzegovina",   # the-odds-api spelling
    "bosnia-herzegovina": "Bosnia-Herzegovina", "bosnia": "Bosnia-Herzegovina",
    "curacao": "Curacao", "curaçao": "Curacao",
    "iran": "Iran", "ir iran": "Iran",
}


def _key(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().strip().lower()
    return " ".join(s.split())


def normalize(name: str | None) -> str | None:
    """Return the canonical team name; unknown names pass through (title-trimmed)."""
    if not name:
        return name
    return _ALIASES.get(_key(name), name.strip())
