"""Edge cases: catch-up scheduling, idempotency, name normalization, devig guards,
budget pre-check, preflight."""
import pytest
from datetime import datetime, timezone, timedelta
from schedule.scheduler import due_jobs
from core.data.teams import normalize
from core.data.oddsapi import devig
from core.obs.cost import CostLedger
from core.obs.runs import RunLedger


def _m(mid, mins):
    return {"match_id": mid,
            "utc_kickoff": (datetime.now(timezone.utc) + timedelta(minutes=mins)).isoformat()}


# --- catch-up: a window missed during downtime still fires (within grace) ---
def test_catchup_fires_recently_missed_window():
    # kickoff in 50 min -> the T-60m window opened 10 min ago; must still fire
    due = due_jobs([_m(1, 50)])
    assert any(j["window"] == "T-60m" for j in due)


def test_ancient_window_not_fired():
    # kickoff in 50 min: T-24h opened ~23h ago -> too stale, must NOT fire
    due = due_jobs([_m(1, 50)])
    assert not any(j["window"] == "T-24h" for j in due)


def test_no_fire_after_kickoff():
    assert due_jobs([_m(1, -5)]) == []          # match already started


def test_idempotency_skips_handled():
    seen = {(1, "T-7m")}
    due = due_jobs([_m(1, 7)], is_done=lambda mid, w: (mid, w) in seen)
    assert not any(j["window"] == "T-7m" for j in due)


def test_naive_kickoff_is_coerced_utc():
    naive = {"match_id": 1,
             "utc_kickoff": (datetime.now(timezone.utc) + timedelta(minutes=7))
             .replace(tzinfo=None).isoformat()}
    assert any(j["window"] == "T-7m" for j in due_jobs([naive]))   # no crash, fires


# --- team normalization ---
@pytest.mark.parametrize("raw,canon", [
    ("Korea Republic", "South Korea"), ("Cabo Verde", "Cape Verde"),
    ("Türkiye", "Türkiye"), ("Turkey", "Türkiye"), ("DR Congo", "Congo DR"),
    ("Côte d'Ivoire", "Ivory Coast"), ("USA", "United States"),
    ("Czech Republic", "Czechia"), ("Spain", "Spain")])
def test_normalize(raw, canon):
    assert normalize(raw) == canon


# --- devig guards ---
def test_devig_normalizes():
    p = devig({"H": 2.0, "D": 3.0, "A": 5.0})
    assert abs(sum(p.values()) - 1.0) < 1e-9


def test_devig_rejects_garbage():
    for bad in ({"H": 0, "D": 0, "A": 0}, {"H": 2.0}, {}, None):
        with pytest.raises(ValueError):
            devig(bad)


# --- budget pre-check ---
def test_over_budget():
    led = CostLedger(":memory:")
    assert led.over_budget("odds_api") is False
    for _ in range(50):
        led.record("odds_api", "odds", units=10)   # 500 == budget
    assert led.over_budget("odds_api") is True


# --- persistent idempotency in run ledger ---
def test_was_handled():
    led = RunLedger(":memory:")
    assert led.was_handled(1, "T-7m") is False
    led.start(1, "T-7m")
    assert led.was_handled(1, "T-7m") is True
