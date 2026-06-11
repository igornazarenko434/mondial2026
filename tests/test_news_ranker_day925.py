"""Day-9.25: tests for the article-relevance ranker."""
from __future__ import annotations
from datetime import datetime, timezone

import pytest

from orchestrator.agents.news_ranker import (
    score_article, rank_articles, dedup_by_url_or_title,
)


NOW = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)


def _art(title, snippet="", url="", date=""):
    return {"title": title, "snippet": snippet, "url": url, "date": date}


def test_specific_team_news_outranks_generic_overview():
    """The 2026-06-10 Mexico-v-SA scenario: a 'FIFA World Cup squads' Wikipedia
    article in Brave's top-N should NOT outrank a specific 'Mexico vs South
    Africa team news' preview from ESPN."""
    wiki = _art(title="2026 FIFA World Cup squads",
                 snippet="The 2026 FIFA World Cup is held in Canada, Mexico, "
                          "and the United States...",
                 url="https://en.wikipedia.org/wiki/2026_FIFA_World_Cup_squads",
                 date="2026-06-11")
    specific = _art(title="Mexico vs South Africa Team News: Injuries, Predicted "
                            "Lineups - ESPN",
                     snippet="Mexico's manager confirmed Edson Alvarez starts on "
                              "the bench due to fitness concerns; South Africa "
                              "remain at full strength.",
                     url="https://espn.com/soccer/preview/_/id/...",
                     date="2026-06-10")
    s_wiki = score_article(wiki, "Mexico", "South Africa", now=NOW)
    s_spec = score_article(specific, "Mexico", "South Africa", now=NOW)
    assert s_spec.score > s_wiki.score, \
        f"specific={s_spec.score}, wiki={s_wiki.score}"


def test_breakdown_explains_score():
    """Audit visibility: the breakdown must list each contribution so an
    operator can SEE why a score got assigned. No black-box numbers."""
    art = _art(title="Mexico vs South Africa Team News - ESPN",
                snippet="South Africa centre-back has been ruled out due to a "
                         "hamstring injury",
                url="https://espn.com/soccer/match/...",
                date="2026-06-11")
    sa = score_article(art, "Mexico", "South Africa", now=NOW)
    labels = [r for r, _ in sa.breakdown]
    assert "both teams in title" in labels
    assert "one team in snippet" in labels or "both teams in snippet" in labels
    assert any("injury" in lbl for lbl in labels)
    assert any("espn" in lbl.lower() for lbl in labels)
    assert any("preview" in lbl.lower() for lbl in labels)


def test_freshness_breaks_ties():
    """All else equal, a 24h-old article scores higher than a 48h-old one."""
    today = _art(title="Mexico v South Africa preview",
                  url="https://goal.com/x",
                  date=NOW.strftime("%Y-%m-%d"))
    yesterday = _art(title="Mexico v South Africa preview",
                       url="https://goal.com/y",
                       date=(NOW.replace(hour=0).strftime("%Y-%m-%d")))
    # Force a 2-day-old article for the yesterday slot
    older = _art(title="Mexico v South Africa preview",
                  url="https://goal.com/z",
                  date="2026-06-09")
    s_now = score_article(today, "Mexico", "South Africa", now=NOW)
    s_old = score_article(older, "Mexico", "South Africa", now=NOW)
    assert s_now.score > s_old.score


def test_trusted_source_bump():
    base = _art(title="World Cup news",
                 url="https://random-blog.example.com/post",
                 date="2026-06-11")
    espn = _art(title="World Cup news",
                 url="https://espn.com/post",
                 date="2026-06-11")
    s_base = score_article(base, "Mexico", "South Africa", now=NOW)
    s_espn = score_article(espn, "Mexico", "South Africa", now=NOW)
    assert s_espn.score - s_base.score >= 3, \
        f"trusted-source bump too small: {s_espn.score} vs {s_base.score}"


def test_generic_source_downweight():
    """Wikipedia / reddit get downweighted because they tend to be
    overview/discussion rather than team-news."""
    wiki = _art(title="South Korea v Czechia preview",
                 url="https://en.wikipedia.org/wiki/x",
                 date="2026-06-11")
    espn = _art(title="South Korea v Czechia preview",
                 url="https://espn.com/x",
                 date="2026-06-11")
    s_wiki = score_article(wiki, "South Korea", "Czechia", now=NOW)
    s_espn = score_article(espn, "South Korea", "Czechia", now=NOW)
    assert s_wiki.score < s_espn.score


def test_team_aliases_match_known_short_forms():
    """South Korea ↔ Korea / KOR / Republic of Korea; United States ↔
    USA / US; Czechia ↔ Czech Republic. Aliases prevent us from missing
    relevant articles that use the short form in their title."""
    kor_short = _art(title="Korea team news, predicted XI",
                       url="https://espn.com/x", date="2026-06-11")
    sa = score_article(kor_short, "South Korea", "Czechia", now=NOW)
    labels = [r for r, _ in sa.breakdown]
    assert "one team in title" in labels or "both teams in title" in labels


def test_rank_articles_returns_sorted_highest_first():
    arts = [
        _art(title="World Cup squads overview",
             url="https://wikipedia.org/x", date="2026-06-11"),
        _art(title="Mexico vs South Africa: lineup, injuries, predicted XI",
             url="https://espn.com/y",
             snippet="Mexico's striker is back from injury; South Africa "
                      "centre-back ruled out.",
             date="2026-06-11"),
        _art(title="2026 FIFA World Cup",
             url="https://wikipedia.org/y", date="2026-06-11"),
    ]
    scored = rank_articles(arts, "Mexico", "South Africa", now=NOW)
    assert "Mexico" in scored[0].title
    # Wikipedia overviews land at the bottom
    assert "wikipedia" in scored[-1].url.lower()


def test_dedup_by_url_or_title_collapses_amp_and_trailing_slash():
    """Brave occasionally returns the same article via /amp/ + plain URL or
    with a trailing slash. Title-similarity dedup catches the cases that
    the URL-exact dedup misses."""
    arts = [
        _art(title="Mexico vs South Africa Preview - Goal", url="https://goal.com/preview"),
        _art(title="Mexico vs South Africa Preview - Goal", url="https://goal.com/preview/"),  # trailing /
        _art(title="Mexico vs South Africa Preview - Goal", url="https://goal.com/preview/amp/"),  # amp variant
        _art(title="Mexico vs South Africa Predictions - ESPN", url="https://espn.com/diff"),
    ]
    out = dedup_by_url_or_title(arts)
    # First Goal preview is kept; the trailing-slash + amp variants get dropped
    # because title is identical. ESPN's different title survives.
    titles = [r["title"] for r in out]
    assert titles.count("Mexico vs South Africa Preview - Goal") == 1
    assert "Mexico vs South Africa Predictions - ESPN" in titles


def test_score_breakdown_renders_for_audit():
    """Each contribution appears in the breakdown list with its label +
    point value, so audit_fired_card / news_inspect can show 'why'."""
    art = _art(title="Mexico vs South Africa: starting XI confirmed",
                snippet="Mexico's manager confirmed Edson Alvarez out due to "
                         "hamstring injury.",
                url="https://espn.com/x",
                date="2026-06-11")
    sa = score_article(art, "Mexico", "South Africa", now=NOW)
    assert sa.score > 0
    assert len(sa.breakdown) >= 4   # plenty of signals fired
    # Each breakdown entry is a (label, points) pair
    for label, pts in sa.breakdown:
        assert isinstance(label, str)
        assert isinstance(pts, (int, float))
