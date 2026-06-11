"""Day-9.25: per-article relevance scoring for the news agent.

Before this module existed, `_fmt_web_results` just took the first 15 results
in Brave's order, with no scoring of how USEFUL each article was for OUR
specific match. Result: a Wikipedia "FIFA World Cup 2026" overview could
displace a Sports-Mole-style "Predicted XIs and team news" preview, simply
because Brave ranked the Wikipedia higher for query relevance.

This ranker scores each article on:
  • Team-name presence in title (+5 each, +3 if both)
  • Team-name presence in snippet (+2 each)
  • Match-news keywords ("lineup", "injury", "ruled out", "confirmed", …)
  • Source authority (whitelist of trusted football publications)
  • Date freshness (within 24h gets a bigger bump than within 7d)
  • Negative markers (Wikipedia overview, generic "tournament info")

Output is a `ScoredArticle` with the original article + a numeric score +
a breakdown (so audit_fired_card / news_inspect can show WHY an article
scored what it did).

Higher-scored articles get LONGER snippets in the LLM context; lowest-scored
get dropped to fit the context cap (vs the old policy of dropping the
last-in-Brave-order).
"""
from __future__ import annotations
import re
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Iterable


# Trusted football-news sources — articles from these get +3 on the score.
# Lower-case substrings matched against the URL host (and title fragment
# when host can't be derived). Order intentionally lists football-specific
# publications first; broad-news sources second.
TRUSTED_SOURCES = (
    "espn.com", "espn.co", "goal.com", "skysports.com", "bbc.co.uk", "bbc.com",
    "theguardian.com", "guardian.com", "foxsports.com", "sportsmole.co.uk",
    "as.com", "marca.com", "sofascore.com", "transfermarkt.com",
    "transfermarkt.us", "rotowire.com", "footballitalia.net", "fourfourtwo.com",
    "fifa.com", "uefa.com", "concacaf.com", "afc.com", "caf-online.com",
    "the-afc.com", "rfef.es", "fa.com", "fff.fr", "dfb.de",
    "sportsillustrated.com", "si.com", "yahoo.com/sports", "athletic.com",
)

# Sources that we LIGHTLY downweight — they tend to be reference / overview
# articles, useful for tournament context but rarely carrying actionable
# team news for a specific fixture.
GENERIC_SOURCES = (
    "en.wikipedia.org", "wikipedia.org",
    "reddit.com",
)

# Keyword sets — match-news signals. Each set contributes once when ANY
# member matches; we don't double-count overlapping keywords.
INJURY_KEYWORDS = (
    "injur", "ruled out", "ruled-out", "doubt",
    "knock", "limped", "withdraw", "out for", "sidelined",
    "fitness test", "calf", "hamstring", "ankle", "groin", "knee",
    "concussion", "suspended", "suspension", "yellow card", "red card",
    "absent", "miss the", "miss out",
)
LINEUP_KEYWORDS = (
    "lineup", "line-up", "line up", "starting xi", "starting eleven",
    "starts at", "starts on", "predicted xi", "predicted eleven",
    "confirmed xi", "team news", "first choice", "first-choice",
    "rest", "rotation", "rotate", "rotated", "bench",
)
TACTICAL_KEYWORDS = (
    "low-block", "low block", "high press", "counter", "park the bus",
    "must win", "must-win", "dead rubber", "qualified", "permutation",
)

# Title patterns that signal a fixture preview (vs. a generic overview)
PREVIEW_TITLE_PATTERNS = (
    re.compile(r"\bvs\b", re.IGNORECASE),
    re.compile(r"\bvs\.\b", re.IGNORECASE),
    re.compile(r"preview", re.IGNORECASE),
    re.compile(r"team news", re.IGNORECASE),
    re.compile(r"prediction", re.IGNORECASE),
)


@dataclass
class ScoredArticle:
    """An article with its computed relevance score and a breakdown of why.

    The breakdown is a list of (reason, points) pairs — so audit_fired_card
    can print "Mexico in title +5, ESPN source +3, injury keyword +3 → 11"
    instead of a black-box number.
    """
    article: dict
    score: float
    breakdown: list[tuple[str, float]] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.article.get("title") or ""

    @property
    def snippet(self) -> str:
        return self.article.get("snippet") or ""

    @property
    def url(self) -> str:
        return self.article.get("url") or ""

    @property
    def date(self) -> str:
        return self.article.get("date") or ""


def _norm(s: str) -> str:
    """Aggressive lower-case + alphanumeric-only normalization for substring
    checks. Lets 'South Korea' match 'south-korea' / 'southkorea' /
    'South-Korea'."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _date_age_days(d: str | None, now: datetime | None = None) -> float | None:
    """Days between article date (YYYY-MM-DD) and now. None if unparseable.
    Used for the freshness bump."""
    if not d:
        return None
    try:
        article = datetime.strptime(d[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - article).total_seconds() / 86400.0)


def _team_aliases(name: str) -> tuple[str, ...]:
    """Common short-form / alias forms for the team-name substring check.
    The normalizer strips punctuation/case, so 'United States' / 'USA' /
    'us' all collapse to a comparable form. We accept the canonical, plus
    a few well-known shorts."""
    name = (name or "").strip()
    extras: dict[str, tuple[str, ...]] = {
        "South Korea":   ("Korea", "KOR", "Republic of Korea"),
        "United States": ("USA", "US", "United States of America"),
        "Czechia":       ("Czech Republic", "Czech"),
        "South Africa":  ("Bafana"),  # nickname for South Africa
        "Cape Verde":    ("Cape Verde Islands", "Cabo Verde"),
        "Saudi Arabia":  ("Saudi"),
        "New Zealand":   ("NZ", "All Whites"),
        "Bosnia & Herzegovina": ("Bosnia", "Herzegovina", "BIH"),
    }
    out = (name, *extras.get(name, ()))
    return tuple(a for a in out if a)


def _source_host(url: str) -> str:
    """Best-effort URL → host. Returns lower-case bare hostname."""
    m = re.match(r"^https?://([^/]+)/?", (url or "").strip())
    if not m:
        return ""
    return m.group(1).lower()


def _matches_any(text_norm: str, keywords: Iterable[str]) -> bool:
    return any(_norm(k) in text_norm for k in keywords)


def score_article(article: dict, home: str, away: str,
                  now: datetime | None = None) -> ScoredArticle:
    """Compute a relevance score for ONE article against THIS match.

    The score is the sum of additive contributions; each contribution gets
    appended to `breakdown` with a short label. Returning the breakdown
    keeps the scoring transparent — audit_fired_card / news_inspect print
    it verbatim so the user sees why article X scored higher than Y.

    Score ranges in practice:
      14+  : top-tier (both teams in title, trusted source, injury news, fresh)
      10-13: strong (1 team in title + match-news keyword + trusted source)
      6-9  : moderate (some signal but generic or off-source)
      0-5  : weak (no team signal in title or snippet)
      < 0  : Wikipedia / Reddit overview about something else
    """
    title = article.get("title") or ""
    snippet = article.get("snippet") or ""
    url = article.get("url") or ""
    date = article.get("date") or ""

    title_n = _norm(title)
    snippet_n = _norm(snippet)
    host = _source_host(url)

    breakdown: list[tuple[str, float]] = []
    score = 0.0

    # ── Team name presence (the biggest single signal) ──
    home_aliases = _team_aliases(home)
    away_aliases = _team_aliases(away)
    home_in_title = any(_norm(a) in title_n for a in home_aliases if a)
    away_in_title = any(_norm(a) in title_n for a in away_aliases if a)
    if home_in_title and away_in_title:
        score += 8; breakdown.append(("both teams in title", 8))
    elif home_in_title or away_in_title:
        score += 5; breakdown.append(("one team in title", 5))
    home_in_snippet = any(_norm(a) in snippet_n for a in home_aliases if a)
    away_in_snippet = any(_norm(a) in snippet_n for a in away_aliases if a)
    if home_in_snippet and away_in_snippet:
        score += 3; breakdown.append(("both teams in snippet", 3))
    elif home_in_snippet or away_in_snippet:
        score += 2; breakdown.append(("one team in snippet", 2))

    # ── Match-news keyword signals ──
    body_n = title_n + snippet_n
    if _matches_any(body_n, INJURY_KEYWORDS):
        score += 3; breakdown.append(("injury/suspension keyword", 3))
    if _matches_any(body_n, LINEUP_KEYWORDS):
        score += 3; breakdown.append(("lineup/XI keyword", 3))
    if _matches_any(body_n, TACTICAL_KEYWORDS):
        score += 2; breakdown.append(("tactical context keyword", 2))

    # ── Source authority ──
    if any(t in host for t in TRUSTED_SOURCES):
        score += 3; breakdown.append((f"trusted source ({host})", 3))
    if any(g in host for g in GENERIC_SOURCES):
        score -= 3; breakdown.append((f"generic source ({host})", -3))

    # ── Title pattern: looks like a fixture preview, not a general overview ──
    if any(p.search(title) for p in PREVIEW_TITLE_PATTERNS):
        score += 2; breakdown.append(("preview-pattern title", 2))

    # ── Freshness — newer articles weighted slightly higher. The recency
    # filter (Brave's 'pw' = past week) already restricts the pool, so this
    # is a tiebreaker rather than a heavy multiplier. ──
    age_days = _date_age_days(date, now=now)
    if age_days is not None:
        if age_days <= 1.0:
            score += 2; breakdown.append(("freshness ≤24h", 2))
        elif age_days <= 2.0:
            score += 1; breakdown.append(("freshness ≤48h", 1))
        # >48h: no penalty (still inside recency window, just no bonus)

    # ── Tournament-overview anti-pattern (heuristic) ──
    if "world cup squads" in title.lower() or "fifa world cup -" in title.lower():
        score -= 2; breakdown.append(("tournament-overview title", -2))

    return ScoredArticle(article=article, score=score, breakdown=breakdown)


def rank_articles(results: list[dict], home: str, away: str,
                  now: datetime | None = None) -> list[ScoredArticle]:
    """Score every article + return them sorted highest-first.
    Stable sort: original Brave order broken only by score deltas."""
    scored = [score_article(r, home, away, now=now) for r in results]
    # Stable sort: Python's sort is stable, so equal scores preserve order
    scored.sort(key=lambda s: s.score, reverse=True)
    return scored


def dedup_by_url_or_title(results: list[dict],
                           title_overlap_chars: int = 60) -> list[dict]:
    """Dedup beyond URL — also collapses near-duplicate titles (case-folded
    prefix). Brave occasionally returns the same article via /amp/, /preview/,
    or with a trailing slash difference; collapsing by title catches those
    too without false positives on different articles that just share a
    common prefix (title_overlap_chars=60 is conservative)."""
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    out: list[dict] = []
    for r in results:
        url = (r.get("url") or "").rstrip("/").lower()
        url_norm = re.sub(r"[?#].*", "", url)
        if url_norm in seen_urls:
            continue
        title_key = _norm((r.get("title") or "")[:title_overlap_chars])
        if title_key and title_key in seen_titles:
            continue
        if url_norm:
            seen_urls.add(url_norm)
        if title_key:
            seen_titles.add(title_key)
        out.append(r)
    return out
