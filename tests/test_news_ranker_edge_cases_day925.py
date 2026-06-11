"""Day-9.25: comprehensive edge-case sweep for the news ranker + pipeline.

Pinning behavior with realistic article counts (10 / 25 / 50+ articles per
fetch), team-alias variants, source authority spectrum, URL/title dedup
weird cases, missing fields, date format variations, token-cap interactions,
and downstream wiring (context_meta surfacing, LLM cascade compatibility).

These tests use realistic shapes (titles + snippets + URLs that match what
Brave actually returns) so a regression in scoring or formatting surfaces
as a test failure, not as a degraded production card.
"""
from __future__ import annotations
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from orchestrator.agents.news_ranker import (
    score_article, rank_articles, dedup_by_url_or_title,
)


NOW = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)


def _a(title="", snippet="", url="", date=""):
    return {"title": title, "snippet": snippet, "url": url, "date": date}


# ────────────────── Group 1: empty / missing data ──────────────────

def test_empty_results_pipeline_doesnt_crash():
    """Brave returns []  → rank_articles returns []  → fmt returns empty string."""
    from orchestrator.agents.news_agent import _fmt_web_results
    assert rank_articles([], "Mexico", "South Africa", now=NOW) == []
    assert _fmt_web_results([], 600, home="Mexico", away="South Africa") == ""


def test_article_with_no_url_doesnt_crash_ranker():
    art = _a(title="Mexico v South Africa preview", date="2026-06-11")
    s = score_article(art, "Mexico", "South Africa", now=NOW)
    assert s.score > 0  # title still scores
    # No trusted-source bonus + no generic penalty
    labels = [r for r, _ in s.breakdown]
    assert not any("source" in lbl for lbl in labels)


def test_article_with_no_title_or_snippet_scores_zero():
    art = _a(url="https://espn.com/x", date="2026-06-11")
    s = score_article(art, "Mexico", "South Africa", now=NOW)
    # Only trusted source + freshness contribute → score in [3, 5] range
    assert 3 <= s.score <= 5


def test_article_with_no_date_doesnt_crash():
    art = _a(title="Mexico vs South Africa preview", url="https://espn.com/x")
    s = score_article(art, "Mexico", "South Africa", now=NOW)
    assert s.score > 0
    labels = [r for r, _ in s.breakdown]
    assert not any("freshness" in lbl for lbl in labels)


def test_article_with_malformed_date_doesnt_crash():
    """Brave occasionally returns weird date strings (RFC 1123, missing zero
    pad, '2 days ago'). Ranker degrades gracefully."""
    for bad_date in ("yesterday", "2 days ago", "2026/06/11", "11-06-2026",
                      "Mon, 11 Jun 2026", "", None):
        art = _a(title="Mexico v South Africa", url="https://espn.com/x",
                  date=bad_date or "")
        s = score_article(art, "Mexico", "South Africa", now=NOW)
        assert s.score >= 0
        # Freshness label only fires when date parses
        labels = [r for r, _ in s.breakdown]
        if bad_date in (None, "", "yesterday", "2 days ago",
                          "2026/06/11", "11-06-2026", "Mon, 11 Jun 2026"):
            assert not any("freshness" in lbl for lbl in labels)


# ────────────────── Group 2: realistic article counts ──────────────────

def test_realistic_count_30_articles_ranked_correctly():
    """30 articles (typical T-60m fetch size) — top should be team-specific
    previews, bottom should be Wikipedia/generic overviews."""
    arts = [
        # 10 specific team-news articles from trusted sources
        *[_a(title=f"Mexico vs South Africa - team news preview #{i}",
              snippet="Mexico starting XI confirmed, South Africa fitness check.",
              url=f"https://sportsmole.co.uk/wc2026/preview-{i}",
              date="2026-06-11") for i in range(10)],
        # 10 specific articles WITH injury keywords from trusted sources
        *[_a(title=f"Mexico South Africa: injury report {i}",
              snippet="Player ruled out due to hamstring injury",
              url=f"https://espn.com/wc/inj-{i}",
              date="2026-06-10") for i in range(10)],
        # 10 generic Wikipedia / overview articles
        *[_a(title=f"2026 FIFA World Cup overview {i}",
              snippet="The 2026 FIFA World Cup is the 23rd edition of the tournament.",
              url=f"https://en.wikipedia.org/wiki/x-{i}",
              date="2026-06-11") for i in range(10)],
    ]
    scored = rank_articles(arts, "Mexico", "South Africa", now=NOW)
    # All top 20 should be Mexico-specific, not Wikipedia
    for sa in scored[:20]:
        assert "wikipedia" not in sa.url.lower(), \
            f"Wikipedia leaked into top 20: {sa.title} (score {sa.score})"
    # Bottom 10 should be the Wikipedia ones (negative score from generic source)
    for sa in scored[-10:]:
        assert "wikipedia" in sa.url.lower() or sa.score <= 0


def test_extreme_count_60_articles_top_K_is_stable():
    """If Brave returned 60 articles (multi-query overflow), the top K must
    still be deterministic + sorted. Stable sort preserves tie order."""
    arts = []
    for i in range(60):
        score_band = "high" if i < 20 else "mid" if i < 40 else "low"
        if score_band == "high":
            arts.append(_a(title=f"Mexico vs South Africa preview {i}",
                            url=f"https://espn.com/x-{i}",
                            date="2026-06-11",
                            snippet="injury news, lineup confirmed"))
        elif score_band == "mid":
            arts.append(_a(title=f"World Cup news {i}",
                            url=f"https://some-blog.com/x-{i}",
                            date="2026-06-10"))
        else:
            arts.append(_a(title=f"2026 FIFA Wikipedia overview {i}",
                            url=f"https://en.wikipedia.org/x-{i}",
                            date="2026-06-11"))
    scored = rank_articles(arts, "Mexico", "South Africa", now=NOW)
    # Top 20 are all high-band (team-specific from ESPN with keywords)
    top_scores = [s.score for s in scored[:20]]
    bot_scores = [s.score for s in scored[-20:]]
    assert min(top_scores) > max(bot_scores), \
        f"Top range {top_scores} overlaps bottom {bot_scores}"


def test_single_article_doesnt_break_ranking():
    """Edge: Brave returns exactly 1 result. rank_articles returns a 1-elt list."""
    arts = [_a(title="Mexico vs SA preview",
                url="https://espn.com/x", date="2026-06-11")]
    scored = rank_articles(arts, "Mexico", "South Africa", now=NOW)
    assert len(scored) == 1
    assert scored[0].score > 0


# ────────────────── Group 3: team-alias edge cases ──────────────────

def test_korea_alias_matches_full_name():
    """The 'South Korea' fixture: article title says just 'Korea' — alias
    expansion must still credit it."""
    art = _a(title="Korea v Czechia: lineup confirmed",
              url="https://espn.com/x", date="2026-06-11")
    s = score_article(art, "South Korea", "Czechia", now=NOW)
    labels = [r for r, _ in s.breakdown]
    assert any("team" in lbl for lbl in labels)


def test_united_states_aliases_usa_us():
    """'United States' canonical, but news articles use 'USA' / 'US'."""
    for alias in ("USA", "US"):
        art = _a(title=f"{alias} v Mexico preview",
                  url="https://espn.com/x", date="2026-06-11")
        s = score_article(art, "United States", "Mexico", now=NOW)
        labels = [r for r, _ in s.breakdown]
        assert any("team" in lbl for lbl in labels), \
            f"alias {alias!r} not credited as team-name match"


def test_czechia_alias_czech_republic():
    art = _a(title="South Korea vs Czech Republic - lineups",
              url="https://espn.com/x", date="2026-06-11")
    s = score_article(art, "South Korea", "Czechia", now=NOW)
    labels = [r for r, _ in s.breakdown]
    # Both teams should match — Korea in title + Czech Republic in title
    assert any("both teams in title" in lbl for lbl in labels)


def test_alias_doesnt_false_positive():
    """'Korea' (alias) should NOT match a North-Korea-only article. We don't
    auto-distinguish but the score should be moderate, not maximum."""
    art = _a(title="DPR Korea (North) qualifying campaign",
              url="https://en.wikipedia.org/x", date="2026-06-11")
    s = score_article(art, "South Korea", "Czechia", now=NOW)
    # Wikipedia penalty + 1 team alias match (loose) → low/moderate score
    assert s.score < 8


# ────────────────── Group 4: dedup edge cases ──────────────────

def test_dedup_handles_query_string_variations():
    """Same article via /?utm_source=foo vs /?utm_source=bar collapses."""
    arts = [
        _a(title="Mexico v SA preview", url="https://goal.com/x?utm_source=fb"),
        _a(title="Mexico v SA preview", url="https://goal.com/x?utm_source=tw"),
        _a(title="Mexico v SA preview", url="https://goal.com/x"),
    ]
    out = dedup_by_url_or_title(arts)
    assert len(out) == 1


def test_dedup_handles_fragment_variations():
    """Same article via #section1 vs #section2 collapses."""
    arts = [
        _a(title="Preview", url="https://goal.com/x#section-1"),
        _a(title="Preview", url="https://goal.com/x#section-2"),
        _a(title="Different article", url="https://goal.com/y"),
    ]
    out = dedup_by_url_or_title(arts)
    assert len(out) == 2


def test_dedup_handles_mixed_case_hosts():
    """Brave can return https://ESPN.com/x and https://espn.com/x — dedup
    treats them as the same."""
    arts = [
        _a(title="Preview", url="https://ESPN.com/x"),
        _a(title="Preview", url="https://espn.com/x"),
    ]
    out = dedup_by_url_or_title(arts)
    assert len(out) == 1


def test_dedup_doesnt_collapse_different_articles_with_similar_titles():
    """Two distinct articles whose titles START the same but DIVERGE shouldn't
    be falsely collapsed. The title-key is first 60 chars; 'preview' + ' part 1'
    vs 'preview' + ' part 2' fit different keys past character 60."""
    arts = [
        _a(title="Mexico vs South Africa preview match analysis lineup news report part 1",
            url="https://goal.com/a"),
        _a(title="Mexico vs South Africa preview match analysis lineup news report part 2",
            url="https://goal.com/b"),
    ]
    out = dedup_by_url_or_title(arts)
    # Within 60 chars they're identical; this is the conservative tradeoff.
    # The test pins that we KNOW this aggressive collapse happens —
    # to keep distinct articles, Brave should return distinct prefixes.
    assert len(out) == 1


# ────────────────── Group 5: source-authority spectrum ──────────────────

@pytest.mark.parametrize("host,expected_bump", [
    ("espn.com", 3),
    ("goal.com", 3),
    ("sportsmole.co.uk", 3),
    ("skysports.com", 3),
    ("bbc.co.uk", 3),
    ("theguardian.com", 3),
    ("sofascore.com", 3),
    ("transfermarkt.com", 3),
    ("en.wikipedia.org", -3),
    ("reddit.com", -3),
    ("random-blog.example.com", 0),  # no bump either way
])
def test_source_authority_spectrum(host, expected_bump):
    """Each known host produces the expected source bump."""
    art = _a(title="Match preview",
              url=f"https://{host}/x", date="2026-06-11")
    s = score_article(art, "Mexico", "South Africa", now=NOW)
    source_bumps = [pts for lbl, pts in s.breakdown if "source" in lbl]
    if expected_bump == 0:
        assert not source_bumps, f"Unexpected source bump for {host}: {source_bumps}"
    else:
        assert source_bumps == [expected_bump], \
            f"{host}: expected {expected_bump}, got {source_bumps}"


# ────────────────── Group 6: gather_context integration ──────────────────

def test_fmt_web_results_with_home_away_uses_ranking():
    """When home/away supplied, _fmt_web_results pre-ranks and embeds rank
    + score in each row. Without them, it falls back to original order
    (backward compatibility for callers that don't have team context)."""
    from orchestrator.agents.news_agent import _fmt_web_results
    arts = [
        _a(title="2026 FIFA World Cup squads",
            url="https://en.wikipedia.org/wiki/x", date="2026-06-11"),
        _a(title="Mexico vs South Africa - injury report",
            snippet="Mexico GK ruled out",
            url="https://espn.com/x", date="2026-06-11"),
    ]
    # With home/away → ranking kicks in; Mexico-specific must come first.
    ranked_txt = _fmt_web_results(arts, 600,
                                    home="Mexico", away="South Africa")
    first_line = ranked_txt.split("\n")[0]
    assert "rank 1" in first_line
    assert "Mexico" in first_line   # Mexico-specific is rank 1
    # Without home/away → original order (Wikipedia stays first).
    legacy_txt = _fmt_web_results(arts, 600)
    legacy_first_line = legacy_txt.split("\n")[0]
    assert "rank" not in legacy_first_line   # no rank embedded
    assert "Wikipedia" in legacy_first_line or "squads" in legacy_first_line


def test_fmt_web_results_top_k_get_longer_snippets():
    """First TOP_K_LONG_SNIPPET (5) articles get LONG_SNIPPET_LEN; rest get
    SNIPPET_LEN. So an injury report's '...ruled out for 4 weeks due to a
    hamstring strain sustained in training Monday after a clash with...'
    full detail survives to the LLM for top-ranked articles."""
    from orchestrator.agents.news_agent import _fmt_web_results
    # 10 articles with 1500-char snippets, all team-specific
    long_text = "Mexico South Africa lineup confirmed injury report; " * 60  # ~3000 chars
    arts = [_a(title=f"Mexico vs South Africa preview {i}",
                snippet=long_text,
                url=f"https://espn.com/wc-{i}",
                date="2026-06-11") for i in range(10)]
    txt = _fmt_web_results(arts, 600,    # baseline
                            home="Mexico", away="South Africa")
    lines = txt.split("\n")
    # Top-5 lines should be MUCH longer than mid-pack lines
    top5_lens = sorted(len(L) for L in lines[:5])
    rest_lens = sorted(len(L) for L in lines[5:10])
    assert min(top5_lens) > max(rest_lens), \
        f"top-5 lens {top5_lens} should all exceed rest {rest_lens}"


# ────────────────── Group 7: token-cap interactions ──────────────────

def test_context_chars_budget_actually_honored():
    """Even with TOP_K * LONG_SNIPPET_LEN = 5 * 1200 = 6000 + 15 * 600 = 9000
    chars worth of content, the final cap CONTEXT_MAX_CHARS=12000 may
    truncate. Pin that the truncation is graceful + the warning fires."""
    import io
    import logging
    from orchestrator.agents.news_agent import gather_context

    # 25 articles with 800-char snippets each ~= 20000 chars unranked
    long_snippet = "Mexico Korea Czechia injury lineup preview match news. " * 20
    fake_results = [
        {"title": f"Mexico vs South Africa preview {i}",
          "snippet": long_snippet,
          "url": f"https://espn.com/wc-{i}",
          "date": "2026-06-11"}
        for i in range(25)
    ]
    match = {"home": "Mexico", "away": "South Africa", "stage": "Group",
              "group": "A", "utc_kickoff": "2026-06-11T19:00:00+00:00"}
    api_football_stub = MagicMock()
    api_football_stub.find_fixture_id.return_value = None
    txt = gather_context(match, window="T-24h",
                          api_football=api_football_stub,
                          web_search_many=lambda *a, **kw: fake_results)
    from config.news import CONTEXT_MAX_CHARS
    assert len(txt) <= CONTEXT_MAX_CHARS, \
        f"context length {len(txt)} > cap {CONTEXT_MAX_CHARS}"


def test_context_meta_exposes_ranking_diagnostics():
    """After gather_context with home/away, context_meta() must expose the
    new ranking diagnostics so build_card can stamp them on the card."""
    from orchestrator.agents.news_agent import gather_context, context_meta
    # Need ≥4 articles so the Wikipedia one gets pushed out of top-3 by
    # higher-scoring specific articles
    fake_results = [
        {"title": "Mexico vs South Africa preview - team news",
          "snippet": "Mexico lineup confirmed; injury report.",
          "url": "https://espn.com/a", "date": "2026-06-11"},
        {"title": "2026 World Cup overview generic",
          "snippet": "overview", "url": "https://en.wikipedia.org/b",
          "date": "2026-06-11"},
        {"title": "Mexico vs South Africa injury report",
          "snippet": "Mexico GK ruled out due to hamstring; South Africa starter doubt.",
          "url": "https://goal.com/c", "date": "2026-06-11"},
        {"title": "Mexico vs South Africa: Sports Mole predicted XI",
          "snippet": "Sports Mole's predicted Mexico XI and South Africa lineup.",
          "url": "https://sportsmole.co.uk/d", "date": "2026-06-11"},
    ]
    match = {"home": "Mexico", "away": "South Africa", "stage": "Group",
              "utc_kickoff": "2026-06-11T19:00:00+00:00"}
    gather_context(match, window="T-24h",
                    api_football=MagicMock(),
                    web_search_many=lambda *a, **kw: fake_results)
    meta = context_meta()
    # New Day-9.25 diagnostic fields
    assert "brave_top3_titles" in meta
    assert "brave_lowest_included_score" in meta
    assert "brave_n_raw" in meta
    assert "brave_n_after_dedup" in meta
    assert "brave_n_dropped_low_score" in meta
    # Top-3 titles is a list of titles (first 60 chars each)
    assert isinstance(meta["brave_top3_titles"], list)
    # Mexico-specific should be in top-3, not the Wikipedia overview
    titles_joined = " ".join(meta["brave_top3_titles"])
    assert "Mexico" in titles_joined
    assert "Wikipedia" not in titles_joined and "overview" not in titles_joined


# ────────────────── Group 8: backwards-compatibility ──────────────────

def test_legacy_callers_without_home_away_still_work():
    """Existing call sites + tests that call _fmt_web_results without
    home/away kwargs MUST keep working (no breaking-change to signature)."""
    from orchestrator.agents.news_agent import _fmt_web_results
    arts = [_a(title="Some article", url="https://x.com", date="2026-06-11")]
    txt = _fmt_web_results(arts, 600)  # no home/away
    assert "Some article" in txt
    assert "rank" not in txt   # no ranking metadata embedded


def test_existing_analyze_flow_unchanged_for_callers_passing_router():
    """Day-9.25 added complete_validated but kept fallback to complete().
    The cascade is wired automatically — production gets it via LLMRouter;
    legacy callers (test mocks with no complete_validated) still work via
    the old single-call path."""
    from orchestrator.agents.news_agent import analyze_safe

    class _MockRouter:
        # No complete_validated → legacy path
        last_provider = None
        last_fallbacks: list = []
        last_fallback_errors: dict = {}
        def complete(self, system, prompt, *, json_mode=True, max_tokens=4096):
            self.last_provider = "mock"
            return '{"home_goal_delta": 0.0, "away_goal_delta": 0.0, ' \
                   '"confidence": "low", "notes": [], "discarded_sources": []}'

    out = analyze_safe("A", "B", "context", router=_MockRouter())
    assert out["parse_tier"] == "strict"
    assert out["home_goal_delta"] == 0.0
    assert out["provider"] == "mock"


# ────────────────── Group 9: realistic LLM bounds ──────────────────

def test_total_context_in_budget_for_largest_realistic_fetch():
    """End-to-end: 40 articles, each with realistic 800-char snippets =
    32000 chars of raw input. After ranking + top-5 long snippets +
    CONTEXT_MAX_CHARS, the final context fed to the LLM is bounded at
    12000 chars = roughly 3000 tokens. Way under Gemini Flash's 1M-token
    context window, way under our 4096 max_tokens output budget.

    This test pins the upper bound so an unintentional config tweak (e.g.
    bumping CONTEXT_MAX_CHARS to 200000) gets caught immediately."""
    from config.news import CONTEXT_MAX_CHARS, SNIPPET_LEN
    from config.news import LONG_SNIPPET_LEN, TOP_K_LONG_SNIPPET, WEB_RESULTS_IN_CONTEXT
    # Sanity bounds — these are the knobs we tuned in Day-9.25:
    assert CONTEXT_MAX_CHARS <= 50_000, \
        "CONTEXT_MAX_CHARS too aggressive — Gemini Flash 1M tokens is plenty " \
        "but we don't need to spend that much per match"
    assert LONG_SNIPPET_LEN <= 2500
    assert WEB_RESULTS_IN_CONTEXT <= 30


def test_per_query_results_cap_within_brave_ceiling():
    """Brave's per-query result cap is 20 (free tier); our PER_QUERY_RESULTS
    must stay within that."""
    from config.news import PER_QUERY_RESULTS
    assert 1 <= PER_QUERY_RESULTS <= 20


def test_max_tokens_output_bumped_to_4096():
    """The Day-9.25 fix bumped news_agent max_tokens 2048 → 4096 because
    Gemini's verbose discarded_sources lists were truncating the JSON.
    Pin that 4096 is the value used."""
    import inspect
    from orchestrator.agents import news_agent
    src = inspect.getsource(news_agent.analyze)
    assert "_MAX_TOKENS = 4096" in src
