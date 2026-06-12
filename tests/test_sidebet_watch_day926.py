"""Day-9.26: side-bet resolution detector tests.

Pin the contract:
  1. Newly-resolved shell → ONE Telegram alert
  2. Already-resolved shell on next tick → no re-alert (idempotent)
  3. Unresolved shells → no alert
  4. Alert body contains question + correctAnswer + paste-ready CLI per friend
  5. SQLite state survives across calls; tracks ALL shells we've seen
"""
from __future__ import annotations
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tools import sidebet_watch


NOW = datetime(2026, 6, 12, 18, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    with open("store/schema.sql") as f:
        c.executescript(f.read())
    return c


def _ntm(shells):
    """Build a fake ntm module returning the given side-bet shells."""
    return SimpleNamespace(
        _read_all=lambda path, **_kw:
            shells if "sideBets" in path else [])


def test_new_resolution_fires_one_telegram(conn):
    sent = []
    shells = [{
        "_path": "tournaments/t1/sideBets/sb_2026-06-11",
        "question": "Mexico - South Africa total goals over 2.5",
        "correctAnswer": "No",
        "isResolved": True,
    }]
    out = sidebet_watch.detect_and_alert(
        conn, "t1", _ntm(shells),
        tracked_names=["Igor", "Vaadia"],
        send_telegram=lambda title, body:
            (sent.append((title, body)), True)[1],
        now=NOW)

    assert out["detected"] == ["sb_2026-06-11"]
    assert out["alerted"] == ["sb_2026-06-11"]
    assert len(sent) == 1
    title, body = sent[0]
    assert "Side bet resolved" in title
    assert "Mexico - South Africa total goals over 2.5" in body
    assert "Correct answer: No" in body
    assert 'side-bet "Igor"' in body
    assert 'side-bet "Vaadia"' in body


def test_already_resolved_doesnt_re_alert(conn):
    """Second call sees same state → ZERO alerts. Idempotency holds."""
    sent = []
    shells = [{
        "_path": "tournaments/t1/sideBets/sb_2026-06-11",
        "question": "Mexico v SA over 2.5",
        "correctAnswer": "No",
        "isResolved": True,
    }]
    fake = _ntm(shells)
    # First call: fires
    sidebet_watch.detect_and_alert(
        conn, "t1", fake, tracked_names=["Igor"],
        send_telegram=lambda t, b: (sent.append((t, b)), True)[1], now=NOW)
    # Second call (same data): no new alert
    sent.clear()
    out2 = sidebet_watch.detect_and_alert(
        conn, "t1", fake, tracked_names=["Igor"],
        send_telegram=lambda t, b: (sent.append((t, b)), True)[1], now=NOW)
    assert out2["detected"] == []
    assert out2["alerted"] == []
    assert sent == []


def test_unresolved_shell_doesnt_alert(conn):
    """A side bet that hasn't resolved yet stays silent."""
    sent = []
    shells = [{
        "_path": "tournaments/t1/sideBets/sb_pending",
        "question": "Brazil v Morocco — Diaz + Hakimi goals?",
        "isResolved": False,
    }]
    out = sidebet_watch.detect_and_alert(
        conn, "t1", _ntm(shells), tracked_names=["Igor"],
        send_telegram=lambda t, b: (sent.append((t, b)), True)[1], now=NOW)
    assert out["detected"] == []
    assert sent == []


def test_transition_from_unresolved_to_resolved_alerts(conn):
    """First tick: unresolved (no alert). Second tick: now resolved → alert."""
    sent = []
    # Tick 1: unresolved
    shells_pre = [{
        "_path": "tournaments/t1/sideBets/sb_x",
        "question": "Q?", "isResolved": False}]
    sidebet_watch.detect_and_alert(
        conn, "t1", _ntm(shells_pre), tracked_names=["Igor"],
        send_telegram=lambda t, b: (sent.append((t, b)), True)[1], now=NOW)
    assert sent == []

    # Tick 2: now resolved → alert fires
    shells_post = [{
        "_path": "tournaments/t1/sideBets/sb_x",
        "question": "Q?", "correctAnswer": "Yes", "isResolved": True}]
    out = sidebet_watch.detect_and_alert(
        conn, "t1", _ntm(shells_post), tracked_names=["Igor"],
        send_telegram=lambda t, b: (sent.append((t, b)), True)[1], now=NOW)
    assert out["alerted"] == ["sb_x"]
    assert len(sent) == 1
    assert "Yes" in sent[0][1]


def test_cumulative_count_grows_with_each_resolution(conn):
    """Body's CLI command uses cumulative count: the operator sets the
    running total, not the delta. First side bet → set to 1; second → 2."""
    sent = []
    fake_send = lambda t, b: (sent.append(b), True)[1]
    # First resolution
    sidebet_watch.detect_and_alert(
        conn, "t1",
        _ntm([{"_path": "tournaments/t1/sideBets/sb1",
                "question": "Q1", "correctAnswer": "Yes",
                "isResolved": True}]),
        tracked_names=["Igor"], send_telegram=fake_send, now=NOW)
    assert "side-bet \"Igor\" 1" in sent[0]

    # Second resolution (different sb_id, new transition)
    sent.clear()
    sidebet_watch.detect_and_alert(
        conn, "t1",
        _ntm([
            {"_path": "tournaments/t1/sideBets/sb1",
             "question": "Q1", "correctAnswer": "Yes", "isResolved": True},
            {"_path": "tournaments/t1/sideBets/sb2",
             "question": "Q2", "correctAnswer": "No", "isResolved": True},
        ]),
        tracked_names=["Igor"], send_telegram=fake_send, now=NOW)
    assert "side-bet \"Igor\" 2" in sent[0]


def test_telegram_failure_doesnt_kill_subsequent_alerts(conn):
    """If the Telegram for shell A fails, shell B should still get alerted."""
    sent = []
    call_count = [0]
    def flaky_send(t, b):
        call_count[0] += 1
        if call_count[0] == 1:
            return False     # First send fails
        sent.append(b)
        return True
    shells = [
        {"_path": "tournaments/t1/sideBets/sbA", "question": "QA",
         "correctAnswer": "Yes", "isResolved": True},
        {"_path": "tournaments/t1/sideBets/sbB", "question": "QB",
         "correctAnswer": "No", "isResolved": True},
    ]
    out = sidebet_watch.detect_and_alert(
        conn, "t1", _ntm(shells), tracked_names=["Igor"],
        send_telegram=flaky_send, now=NOW)
    assert out["detected"] == ["sbA", "sbB"]
    assert out["alerted"] == ["sbB"]   # only B alerted, A's send returned False
    assert call_count[0] == 2
    # And there's an error recorded for A
    assert any("sbA" in e for e in out.get("errors", []))


def test_uses_my_participant_and_friend_participants_env(conn, monkeypatch):
    """When tracked_names is None, defaults to MY_PARTICIPANT + FRIEND_PARTICIPANTS."""
    sent = []
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.setenv("FRIEND_PARTICIPANTS", "Vaadia, Tal")
    shells = [{
        "_path": "tournaments/t1/sideBets/sb_x",
        "question": "Q?", "correctAnswer": "Yes", "isResolved": True}]
    sidebet_watch.detect_and_alert(
        conn, "t1", _ntm(shells),
        send_telegram=lambda t, b: (sent.append(b), True)[1], now=NOW)
    body = sent[0]
    assert 'side-bet "Igor"' in body
    assert 'side-bet "Vaadia"' in body
    assert 'side-bet "Tal"' in body


def test_state_persists_question_correct_answer_seen_at(conn):
    """The state table records the data we need for forensics."""
    shells = [{
        "_path": "tournaments/t1/sideBets/sb_x",
        "question": "Mexico v SA over 2.5",
        "correctAnswer": "No", "isResolved": True}]
    sidebet_watch.detect_and_alert(
        conn, "t1", _ntm(shells), tracked_names=["Igor"],
        send_telegram=lambda t, b: True, now=NOW)
    row = conn.execute(
        "SELECT question, correct_answer, is_resolved, notified_at, seen_at "
        "FROM side_bet_state WHERE side_bet_id='sb_x'").fetchone()
    assert row["question"] == "Mexico v SA over 2.5"
    assert row["correct_answer"] == "No"
    assert row["is_resolved"] == 1
    assert row["notified_at"]                          # was alerted
    assert row["seen_at"]
