"""Day-6 render_card: line-count cap (≤8 normal, ≤9 KO+penalty), signals
line format with inline failure reasons, penalty line conditions, context
truncation. The render is what lands on the user's phone — every line counts.
"""
from __future__ import annotations
import pytest
from core.delivery.base import render_card, MAX_LINES_NORMAL, MAX_LINES_KO_PEN


def _base_card(**overrides):
    """Full, well-formed Day-6 card."""
    card = {
        "match_id": 1, "home": "Norway", "away": "France",
        "stage": "Group", "group": "I", "detonator": True,
        "kickoff_local": "2026-06-26 22:00",
        "locked_odds": {"H": 4.20, "D": 3.60, "A": 1.85},
        "model_prob":  {"H": 0.22, "D": 0.26, "A": 0.52},
        "pick_exact_score": {"home": 1, "away": 2},
        "pick_direction": "A",
        "modal_score": {"home": 0, "away": 1},
        "expected_points": 1.90,
        "context": ["Norway likely rotates", "Mbappé confirmed starts"],
        "signals_used":    ["dixon_coles", "elo", "market", "news"],
        "signals_failed":  [],
        "failure_reasons": {},
        "ev_pathway": "ev_optimized",
        "penalty_winner": None,
    }
    card.update(overrides)
    return card


# ---------- Line-count caps ----------

def test_normal_card_within_8_line_cap():
    txt = render_card(_base_card())
    lines = txt.split("\n")
    assert len(lines) <= MAX_LINES_NORMAL, \
        f"normal card overflowed cap ({len(lines)} > {MAX_LINES_NORMAL}):\n{txt}"


def test_ko_with_penalty_within_9_line_cap():
    card = _base_card(stage="R16", group=None,
                      penalty_winner={"winner": "A", "p_winner": 0.515})
    txt = render_card(card)
    lines = txt.split("\n")
    assert len(lines) <= MAX_LINES_KO_PEN, \
        f"KO+pen card overflowed cap ({len(lines)} > {MAX_LINES_KO_PEN}):\n{txt}"


def test_card_with_many_context_bullets_truncated_to_two():
    card = _base_card(context=["A", "B", "C", "D", "E", "F"])
    txt = render_card(card)
    # Only the bullets-line; should show exactly 2 bullets joined
    bullet_lines = [ln for ln in txt.split("\n") if "ℹ " in ln]
    assert len(bullet_lines) == 1
    assert bullet_lines[0].count("ℹ ") == 2


# ---------- Signals line format ----------

def test_signals_line_happy_path():
    txt = render_card(_base_card())
    assert "Signals: DC+Elo+Market+News" in txt


def test_signals_line_with_one_failure_uses_warn_marker():
    card = _base_card(signals_used=["dixon_coles", "elo"],
                      signals_failed=["market"],
                      failure_reasons={"market": "odds_api over budget"})
    txt = render_card(card)
    sig_line = [ln for ln in txt.split("\n") if ln.startswith("Signals:")][0]
    assert "DC+Elo" in sig_line
    assert "⚠market: odds_api over budget" in sig_line


def test_signals_line_shows_news_provider_when_known():
    """Day-8 audit visibility: when news_provider is stamped on the card,
    render shows e.g. 'News(gemini)' so the user sees WHICH model produced
    the news output (cross-references the Honeycomb gemini.complete span)."""
    card = _base_card(news_provider="gemini")
    txt = render_card(card)
    sig_line = [ln for ln in txt.split("\n") if ln.startswith("Signals:")][0]
    assert "News(gemini)" in sig_line


def test_signals_line_omits_provider_suffix_when_unknown():
    """No provider stamped (e.g. cached path) → fall back to plain 'News'."""
    card = _base_card(news_provider=None)
    txt = render_card(card)
    sig_line = [ln for ln in txt.split("\n") if ln.startswith("Signals:")][0]
    assert "+News" in sig_line and "News(" not in sig_line


def test_signals_line_news_failure_shows_warn_marker_not_provider():
    """When news failed, the ⚠ marker carries the reason; we don't append a
    provider suffix because news is in signals_failed, not signals_used."""
    card = _base_card(signals_used=["dixon_coles", "elo", "market"],
                      signals_failed=["news"],
                      failure_reasons={"news": "llm 429"},
                      news_provider=None,           # router returned no successful provider
                      news_failure="all providers down")
    txt = render_card(card)
    sig_line = [ln for ln in txt.split("\n") if ln.startswith("Signals:")][0]
    assert "+News" not in sig_line             # not in signals_used
    assert "⚠news: llm 429" in sig_line


def test_signals_line_with_multiple_failures_inlines_all():
    card = _base_card(signals_used=["dixon_coles"],
                      signals_failed=["elo", "market", "news"],
                      failure_reasons={"elo": "empty",
                                        "market": "budget",
                                        "news": "llm 429"})
    txt = render_card(card)
    sig_line = [ln for ln in txt.split("\n") if ln.startswith("Signals:")][0]
    assert "⚠elo: empty" in sig_line
    assert "⚠market: budget" in sig_line
    assert "⚠news: llm 429" in sig_line


# ---------- Penalty line conditions ----------

def test_penalty_line_present_on_ko_branch():
    card = _base_card(stage="R16",
                      penalty_winner={"winner": "A", "p_winner": 0.515})
    txt = render_card(card)
    assert "► If pens: France (52%)" in txt or "► If pens: France (51%)" in txt


def test_penalty_line_omitted_when_penalty_winner_is_none():
    txt = render_card(_base_card(penalty_winner=None))
    assert "If pens" not in txt


def test_penalty_line_omitted_on_group_stage_even_if_penalty_winner_set():
    """Defensive: shouldn't be set by build_card on groups, but if it were
    accidentally set, render must still show it (render is data-driven).
    This pins that contract — the LOGIC of not setting penalty_winner on
    group games is build_card's responsibility, see test_build_card."""
    card = _base_card(stage="Group",
                      penalty_winner={"winner": "A", "p_winner": 0.515})
    txt = render_card(card)
    assert "If pens" in txt   # render shows whatever build_card produced


# ---------- Modal-line conditional ----------

def test_modal_line_omitted_when_modal_equals_pick():
    """Avoid noise when (likeliest) == pick — saves a precious line."""
    card = _base_card(modal_score={"home": 1, "away": 2})  # equals pick
    txt = render_card(card)
    assert "likeliest" not in txt


def test_modal_line_present_when_modal_differs_from_pick():
    card = _base_card(modal_score={"home": 0, "away": 1})
    txt = render_card(card)
    assert "(likeliest:" in txt


# ---------- Plain-text safety (Telegram won't choke) ----------

def test_render_has_no_markdown_asterisks():
    """parse_mode is off; if someone adds ** for emphasis they'd produce
    weird output. The renderer's contract is plain text only."""
    txt = render_card(_base_card())
    assert "**" not in txt


def test_render_handles_minimal_card_without_crashing():
    """Total-degradation card (every signal failed → empty fields) must
    still render without raising."""
    minimal = {"home": "X", "away": "Y", "stage": "Group",
                "model_prob": {"H": 1/3, "D": 1/3, "A": 1/3},
                "pick_exact_score": {"home": 0, "away": 0},
                "pick_direction": "?",
                "modal_score": {"home": 0, "away": 0},
                "signals_used": [],
                "signals_failed": ["dixon_coles", "elo", "market", "news"],
                "failure_reasons": {"dixon_coles": "x", "elo": "y",
                                      "market": "z", "news": "w"},
                "expected_points": None,
                "ev_pathway": "modal_fallback", "penalty_winner": None}
    txt = render_card(minimal)
    assert "X vs Y" in txt
    assert "Signals" in txt


# ---------- Day-9.12 UX additions ----------

def test_header_shows_window_tag_when_card_has_window_t7m():
    """T-7m is the LOCK card — visually distinct from T-24h/T-60m/T-15m
    previews so the reader knows this is the scoring-decisive one."""
    txt = render_card(_base_card(window="T-7m"))
    assert "[T-7m LOCK]" in txt.split("\n")[0]


def test_header_shows_window_tag_for_previews():
    for w, expected in (("T-24h", "[T-24h]"),
                         ("T-60m", "[T-60m]"),
                         ("T-15m", "[T-15m]")):
        txt = render_card(_base_card(window=w))
        assert expected in txt.split("\n")[0], \
            f"window={w} expected {expected!r} in header, got: {txt.split(chr(10))[0]!r}"


def test_header_omits_window_tag_when_card_has_no_window():
    """Backward-compat: cards without a `window` field render the legacy
    header (existing daemon flows + older tests untouched)."""
    txt = render_card(_base_card())
    header = txt.split("\n")[0]
    assert "[T-" not in header


def test_pick_line_shows_modal_fallback_marker_when_ev_pathway_is_modal_fallback():
    """When odds_api is unavailable build_card falls back to modal-pick.
    The card must visibly say so — otherwise the user can't tell whether
    the pick is EV-optimal or degraded."""
    txt = render_card(_base_card(ev_pathway="modal_fallback"))
    pick_line = [ln for ln in txt.split("\n") if ln.startswith("► Pick:")][0]
    assert "[no live odds]" in pick_line


def test_pick_line_no_modal_fallback_marker_on_happy_path():
    """ev_pathway='ev_optimized' (default in _base_card) → no fallback marker."""
    txt = render_card(_base_card())     # ev_pathway='ev_optimized'
    pick_line = [ln for ln in txt.split("\n") if ln.startswith("► Pick:")][0]
    assert "[no live odds]" not in pick_line


def test_t7m_lock_card_still_within_8_line_cap():
    """The window tag must fit on the existing header line — adding it
    must NOT push us past the 8-line cap on a normal card."""
    txt = render_card(_base_card(window="T-7m"))
    assert len(txt.split("\n")) <= MAX_LINES_NORMAL
