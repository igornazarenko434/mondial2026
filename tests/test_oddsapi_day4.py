"""Day-4 odds wiring: event→fixture matching, book preference, consensus
fallback, budget guard, snapshot persistence. All fully offline — no
real HTTP calls; only the pure logic + a mocked-HTTP integration test."""
from __future__ import annotations
import sqlite3
from unittest.mock import patch

import pytest
from core.data import oddsapi as oa


# ---------- Fixtures ----------

def _event(home="Mexico", away="South Africa",
           commence_time="2026-06-11T19:00:00Z", books=None):
    return {"home_team": home, "away_team": away,
            "commence_time": commence_time, "bookmakers": list(books or [])}


def _book(key, h=1.85, d=3.60, a=4.20,
          home="Mexico", away="South Africa"):
    return {"key": key, "markets": [{"key": "h2h", "outcomes": [
        {"name": home, "price": h},
        {"name": "Draw", "price": d},
        {"name": away, "price": a}]}]}


# ---------- match_event_to_fixture ----------

def test_match_event_canonicalizes_aliases():
    """Cross-source aliases must canonicalize on BOTH sides so the join works
    regardless of which spelling the-odds-api uses for a team."""
    events = [_event(home="Korea Republic", away="Cape Verde Islands",
                     commence_time="2026-07-05T19:00:00Z")]
    found = oa.match_event_to_fixture(
        events, "South Korea", "Cape Verde",
        kickoff_utc="2026-07-05T19:00:00Z")
    assert found is not None
    assert found["home_team"] == "Korea Republic"


def test_match_event_rejects_other_competition_by_date_window():
    """Mexico vs South Africa happening months later (a friendly) must NOT
    match our WC fixture from 2026-06-11."""
    events = [_event(commence_time="2026-09-15T19:00:00Z")]
    assert oa.match_event_to_fixture(
        events, "Mexico", "South Africa",
        kickoff_utc="2026-06-11T19:00:00Z") is None


def test_match_event_skips_date_filter_when_no_kickoff_given():
    """Backward compat: legacy callers without kickoff_utc still match by name."""
    events = [_event(commence_time="2026-09-15T19:00:00Z")]
    found = oa.match_event_to_fixture(events, "Mexico", "South Africa",
                                       kickoff_utc=None)
    assert found is not None


def test_match_event_returns_none_when_no_match():
    events = [_event(home="Mexico", away="Canada")]
    assert oa.match_event_to_fixture(events, "Mexico", "South Africa",
                                      kickoff_utc=None) is None


# ---------- pick_book ----------

def test_pick_book_prefers_pinnacle_over_other_books():
    """Pinnacle is the sharpest market — must be picked when listed."""
    ev = _event(books=[_book("draftkings", h=2.00, d=3.40, a=3.90),
                       _book("pinnacle"),
                       _book("betfair_ex_eu", h=1.87, d=3.55, a=4.10)])
    pick = oa.pick_book(ev)
    assert pick is not None
    book, odds = pick
    assert book == "pinnacle"
    assert odds == {"H": 1.85, "D": 3.60, "A": 4.20}


def test_pick_book_falls_to_betfair_when_pinnacle_absent():
    ev = _event(books=[_book("draftkings"), _book("betfair_ex_eu")])
    pick = oa.pick_book(ev)
    assert pick is not None
    assert pick[0] == "betfair_ex_eu"


def test_pick_book_returns_none_when_no_preferred_book_listed():
    """Triggers the consensus fallback in fetch_match_odds."""
    ev = _event(books=[_book("draftkings"), _book("fanduel")])
    assert oa.pick_book(ev) is None


def test_pick_book_skips_market_with_missing_outcome():
    """If a preferred book is listed but its h2h market is malformed
    (Draw missing, etc.), we should not return broken odds."""
    bad_book = {"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
        {"name": "Mexico", "price": 1.85},
        # no Draw, no away — incomplete
    ]}]}
    ev = _event(books=[bad_book])
    assert oa.pick_book(ev) is None


# ---------- consensus_book ----------

def test_consensus_book_devigs_and_averages_two_books():
    """Two books with slightly different vigs → consensus produces sensible
    decimal odds that fall between the inputs."""
    ev = _event(books=[_book("a", h=2.0, d=3.0, a=4.0),
                       _book("b", h=2.2, d=3.2, a=3.8)])
    pick = oa.consensus_book(ev)
    assert pick is not None
    book, odds = pick
    assert book == "consensus"
    # devigged probabilities sum back to ~1, so decimals are in plausible band
    assert 1.5 < odds["H"] < 3.0
    assert 2.5 < odds["D"] < 4.5
    assert 2.5 < odds["A"] < 5.0


def test_consensus_book_returns_none_when_no_books_have_h2h():
    """No markets → no consensus."""
    ev = _event(books=[{"key": "foo", "markets": []}])
    assert oa.consensus_book(ev) is None


# ---------- fetch_match_odds (orchestration) ----------

def test_fetch_match_odds_uses_passed_events_no_http(monkeypatch):
    """Batch path: caller passes events= so one fetch_all_odds serves many
    matches. We must NOT hit the API a second time."""
    events = [_event(books=[_book("pinnacle")])]
    monkeypatch.setattr(
        "core.obs.cost.ledger",
        lambda: type("L", (), {"over_budget": staticmethod(lambda p: False)})())
    with patch.object(oa, "fetch_all_odds") as m_fetch:
        m_fetch.side_effect = AssertionError("must not call API when events given")
        out = oa.fetch_match_odds("Mexico", "South Africa",
                                   kickoff_utc="2026-06-11T19:00:00Z",
                                   events=events)
    assert out is not None
    assert out["book"] == "pinnacle"
    assert out["H"] == 1.85


def test_fetch_match_odds_returns_none_when_over_budget(monkeypatch):
    """Budget guard must short-circuit before any HTTP call."""
    monkeypatch.setattr(
        "core.obs.cost.ledger",
        lambda: type("L", (), {"over_budget": staticmethod(lambda p: True)})())
    with patch.object(oa, "fetch_all_odds") as m_fetch:
        m_fetch.side_effect = AssertionError("must not call API when over budget")
        assert oa.fetch_match_odds("Mexico", "South Africa", events=None) is None


def test_fetch_match_odds_falls_back_to_consensus(monkeypatch):
    """If none of the preferred books are listed, the consensus average
    decimal is returned with book='consensus'."""
    events = [_event(books=[_book("dk", h=2.0, d=3.0, a=4.0),
                            _book("fd", h=2.2, d=3.2, a=3.8)])]
    monkeypatch.setattr(
        "core.obs.cost.ledger",
        lambda: type("L", (), {"over_budget": staticmethod(lambda p: False)})())
    out = oa.fetch_match_odds("Mexico", "South Africa", events=events)
    assert out is not None
    assert out["book"] == "consensus"


def test_fetch_match_odds_returns_none_when_no_event_matches(monkeypatch):
    monkeypatch.setattr(
        "core.obs.cost.ledger",
        lambda: type("L", (), {"over_budget": staticmethod(lambda p: False)})())
    events = [_event(home="Spain", away="Brazil")]
    assert oa.fetch_match_odds("Mexico", "South Africa", events=events) is None


# ---------- snapshot persistence ----------

def _schema_conn():
    """In-memory DB with the project's real schema applied."""
    conn = sqlite3.connect(":memory:")
    with open("store/schema.sql") as f:
        conn.executescript(f.read())
    return conn


def test_snapshot_odds_writes_then_upserts():
    conn = _schema_conn()
    oa.snapshot_odds(conn, 401, "T-7m", "pinnacle",
                      {"H": 1.85, "D": 3.60, "A": 4.20})
    row = conn.execute(
        "SELECT odds_h, odds_d, odds_a FROM odds_snapshots "
        "WHERE match_id=401 AND captured_at='T-7m' AND book='pinnacle'"
    ).fetchone()
    assert row == (1.85, 3.60, 4.20)

    # Re-snapshot the same (match, window, book) — upsert, not duplicate.
    oa.snapshot_odds(conn, 401, "T-7m", "pinnacle",
                      {"H": 1.90, "D": 3.50, "A": 4.10})
    rows = conn.execute(
        "SELECT odds_h FROM odds_snapshots "
        "WHERE match_id=401 AND captured_at='T-7m' AND book='pinnacle'"
    ).fetchall()
    assert rows == [(1.90,)]


def test_snapshot_keeps_separate_books_for_same_window():
    """The PRIMARY KEY (match_id, captured_at, book) allows multiple books per
    window — we want Pinnacle AND Betfair both stored for later analysis."""
    conn = _schema_conn()
    oa.snapshot_odds(conn, 401, "T-7m", "pinnacle",
                      {"H": 1.85, "D": 3.60, "A": 4.20})
    oa.snapshot_odds(conn, 401, "T-7m", "betfair_ex_eu",
                      {"H": 1.87, "D": 3.55, "A": 4.10})
    cnt = conn.execute(
        "SELECT COUNT(*) FROM odds_snapshots WHERE match_id=401 AND captured_at='T-7m'"
    ).fetchone()[0]
    assert cnt == 2


def test_latest_snapshot_prefers_pinnacle_over_consensus():
    """Read path: when both Pinnacle and consensus exist for a match,
    Pinnacle is the sharpest and must be picked."""
    conn = _schema_conn()
    oa.snapshot_odds(conn, 401, "T-7m", "consensus",
                      {"H": 1.90, "D": 3.50, "A": 4.10})
    oa.snapshot_odds(conn, 401, "T-7m", "pinnacle",
                      {"H": 1.85, "D": 3.60, "A": 4.20})
    snap = oa.latest_snapshot(conn, 401)
    assert snap is not None
    assert snap["book"] == "pinnacle"
    assert snap["H"] == 1.85


def test_latest_snapshot_returns_none_for_unknown_match():
    conn = _schema_conn()
    assert oa.latest_snapshot(conn, 999) is None
