"""Day-9.25: pin the departed-member reconciliation.

Live evidence (2026-06-11): our standings table had 66 rows but Negev's
roster had 65 humans, because:
  - "Yahav" and "yahav sarfati" persisted as phantoms (display-name
    rename produced a duplicate that never got cleaned up)
  - "Shuki" had just joined Negev and the sync hadn't run yet

Phantoms pollute leaderboard counts AND the strategy-tilt gap-to-leader
math. These tests pin the reconciliation contract:

  1. A row in DB but NOT in Negev's current roster → DELETED
  2. The user's own row (MY_PARTICIPANT) is NEVER deleted, even if
     name-matching gets fuzzy
  3. Departed members surface in the return value `departed_members`
     for Telegram visibility
  4. A FAILED Negev fetch must NOT wipe the table (data preservation)
  5. dry-run never deletes anything
"""
from __future__ import annotations
import sqlite3
from types import SimpleNamespace

import pytest

from tools import sync_negev_standings as sns


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    with open("store/schema.sql") as f:
        c.executescript(f.read())
    return c


@pytest.fixture
def fake_ntm():
    def make(rows):
        return SimpleNamespace(
            toto_get_standings=lambda *, tournament_id, include_bots=False:
                [r for r in rows if include_bots or r.get("role") != "bot"])
    return make


def _row(name, total=0):
    return {"player": name, "rank": 1, "total": total, "direction": total,
            "broad": 0, "exactCount": 0, "role": "player"}


def test_departed_member_removed_from_db(conn, fake_ntm, monkeypatch):
    """Phantom Yahav scenario. Seed DB with [Alice, Yahav, Igor]; Negev
    returns only [Alice, Igor]. After sync, Yahav must be GONE from DB,
    `departed_members` must report it."""
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    seed = [_row("Alice", 10), _row("Yahav", 5), _row("Igor", 0)]
    sns.sync_standings(tournament_id="tid", conn=conn, ntm=fake_ntm(seed))
    rows_after_seed = [r[0] for r in conn.execute(
        "SELECT participant FROM standings").fetchall()]
    assert "Yahav" in rows_after_seed                  # initial seed worked

    # Now Negev only has Alice + Igor (Yahav left)
    negev_now = [_row("Alice", 12), _row("Igor", 0)]
    out = sns.sync_standings(tournament_id="tid", conn=conn,
                              ntm=fake_ntm(negev_now))
    assert "Yahav" in (out.get("departed_members") or [])
    rows_final = [r[0] for r in conn.execute(
        "SELECT participant FROM standings").fetchall()]
    assert "Yahav" not in rows_final, \
        "Yahav phantom row should have been deleted"
    assert set(rows_final) == {"Alice", "Igor"}


def test_my_participant_never_deleted_even_if_absent_from_negev(
        conn, fake_ntm, monkeypatch):
    """Safety net: if Igor (MY_PARTICIPANT) is somehow absent from Negev's
    fetched roster (auth weirdness, partial fetch, name mismatch with the
    `displayName`), we must NOT wipe his row from the DB."""
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    # Seed with Igor and one other
    sns.sync_standings(tournament_id="tid", conn=conn,
                        ntm=fake_ntm([_row("Igor", 5), _row("Alice", 10)]))
    # Now Negev returns only Alice — but we MUST preserve Igor locally
    out = sns.sync_standings(tournament_id="tid", conn=conn,
                              ntm=fake_ntm([_row("Alice", 12)]))
    rows = [r[0] for r in conn.execute(
        "SELECT participant FROM standings").fetchall()]
    assert "Igor" in rows, "Igor was deleted even though he's MY_PARTICIPANT"


def test_rename_duplicate_cleanup(conn, fake_ntm, monkeypatch):
    """The 2026-06-11 actual scenario: Negev had 'Yahav HaMeleh' (renamed
    from 'yahav sarfati'). Both old names persist locally as phantoms;
    only the new name is in Negev. Reconciliation removes BOTH old
    forms — only the current name survives."""
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    seed = [_row("Yahav", 5), _row("yahav sarfati", 5),
            _row("Igor", 0)]
    sns.sync_standings(tournament_id="tid", conn=conn, ntm=fake_ntm(seed))

    # Negev now has the new form only
    negev_now = [_row("Yahav HaMeleh", 5), _row("Igor", 0)]
    out = sns.sync_standings(tournament_id="tid", conn=conn,
                              ntm=fake_ntm(negev_now))
    rows = sorted(r[0] for r in conn.execute(
        "SELECT participant FROM standings").fetchall())
    assert rows == ["Igor", "Yahav HaMeleh"]
    assert set(out.get("departed_members") or []) == {"Yahav", "yahav sarfati"}


def test_negev_empty_fetch_does_not_wipe_db(conn, fake_ntm, monkeypatch):
    """Critical safety: a transient Firestore blip or auth fail returning
    zero rows must NOT delete the entire standings table. Without this
    guard, one bad sync could lose the whole leaderboard."""
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    sns.sync_standings(tournament_id="tid", conn=conn,
                        ntm=fake_ntm([_row("Alice", 10), _row("Igor", 0)]))
    rows_before = [r[0] for r in conn.execute(
        "SELECT participant FROM standings").fetchall()]
    assert len(rows_before) == 2

    # Empty Negev fetch (simulates auth fail / Firestore 503)
    out = sns.sync_standings(tournament_id="tid", conn=conn,
                              ntm=fake_ntm([]))
    rows_after = [r[0] for r in conn.execute(
        "SELECT participant FROM standings").fetchall()]
    # Sync returns ok=False and DB is untouched
    assert out.get("ok") is False
    assert rows_after == rows_before


def test_dry_run_does_not_delete(conn, fake_ntm, monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    sns.sync_standings(tournament_id="tid", conn=conn,
                        ntm=fake_ntm([_row("Yahav", 5), _row("Igor", 0)]))
    # Negev no longer has Yahav, but dry-run
    out = sns.sync_standings(tournament_id="tid", conn=conn,
                              ntm=fake_ntm([_row("Igor", 0)]),
                              dry=True)
    rows = [r[0] for r in conn.execute(
        "SELECT participant FROM standings").fetchall()]
    assert "Yahav" in rows, "dry-run should not delete anything"


def test_new_member_added_in_same_sync_that_removes_departed(
        conn, fake_ntm, monkeypatch):
    """The full 2026-06-11 scenario: Shuki JOINS and Yahav LEAVES in the
    same sync. Both deltas surface in the return + are reflected in the DB."""
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    sns.sync_standings(tournament_id="tid", conn=conn,
                        ntm=fake_ntm([_row("Yahav", 5), _row("Igor", 0)]))
    out = sns.sync_standings(tournament_id="tid", conn=conn,
                              ntm=fake_ntm([_row("Shuki", 0), _row("Igor", 0)]))
    rows = sorted(r[0] for r in conn.execute(
        "SELECT participant FROM standings").fetchall())
    assert rows == ["Igor", "Shuki"]
    assert "Shuki" in (out.get("new_members") or [])
    assert "Yahav" in (out.get("departed_members") or [])
