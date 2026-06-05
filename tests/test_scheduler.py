"""Scheduler concurrency, watchdog, and thread-safe ledger writes."""
import threading
import time
from datetime import datetime, timezone, timedelta
from schedule.scheduler import jobs_for_match, due_jobs
from schedule.runner import SchedulerDaemon
from core.obs.cost import CostLedger
from core.obs.runs import RunLedger


def _match(mid, mins_to_ko):
    ko = datetime.now(timezone.utc) + timedelta(minutes=mins_to_ko)
    return {"match_id": mid, "utc_kickoff": ko.isoformat(),
            "home": f"H{mid}", "away": f"A{mid}", "stage": "Group"}


def test_jobs_for_match_has_four_windows():
    jobs = jobs_for_match(_match(1, 100))
    assert {j["window"] for j in jobs} == {"T-24h", "T-60m", "T-15m", "T-7m"}


def test_due_jobs_picks_window():
    # match kicks off in 7 minutes -> the T-7m job is due now
    due = due_jobs([_match(1, 7)])
    assert any(j["window"] == "T-7m" for j in due)


def test_two_simultaneous_matches_run_concurrently(monkeypatch):
    import core.delivery as d
    monkeypatch.setattr(d, "deliver_card", lambda c: True)
    monkeypatch.setattr(d, "alert", lambda t, b: True)

    seen, lock = [], threading.Lock()
    def build(match):
        with lock:
            seen.append(match["match_id"])
        return {"home": match["home"], "away": match["away"], "stage": "Group",
                "pick_exact_score": {"home": 1, "away": 0}, "pick_direction": "H",
                "expected_points": 1.0, "model_prob": {"H": .5, "D": .3, "A": .2},
                "locked_odds": {"H": 2.0, "D": 3.0, "A": 4.0}}

    # two matches both kicking off in 7 min -> both T-7m jobs due at this tick
    matches = [_match(101, 7), _match(202, 7)]
    daemon = SchedulerDaemon(lambda: matches, build, max_workers=2)
    submitted = daemon.tick()
    daemon.pool.shutdown(wait=True)
    assert (101, "T-7m") in submitted and (202, "T-7m") in submitted
    assert set(seen) == {101, 202}            # both pipelines actually ran


def test_idempotent_no_double_dispatch(monkeypatch):
    import core.delivery as d
    monkeypatch.setattr(d, "deliver_card", lambda c: True)
    matches = [_match(1, 7)]
    daemon = SchedulerDaemon(lambda: matches, lambda m: {
        "home": "A", "away": "B", "stage": "Group", "pick_direction": "H",
        "pick_exact_score": {"home": 1, "away": 0}, "expected_points": 1.0,
        "model_prob": {"H": .5, "D": .3, "A": .2},
        "locked_odds": {"H": 2.0, "D": 3.0, "A": 4.0}})
    s1 = daemon.tick(); s2 = daemon.tick()    # same window, second tick
    daemon.pool.shutdown(wait=True)
    assert s1 and not s2                       # dispatched once only


# --- watchdog ---
def test_watchdog_detects_stuck_run():
    led = RunLedger(":memory:")
    led.start(1, "T-7m")                       # never finished
    # force it to look old
    led.conn.execute("UPDATE runs SET started_at=? WHERE id=1",
                     ((datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),))
    led.conn.commit()
    assert len(led.stuck(older_than_min=20)) == 1


# --- thread-safe ledger ---
def test_cost_ledger_thread_safe():
    led = CostLedger(":memory:")
    def worker():
        for _ in range(50):
            led.record("odds_api", "odds", units=1)
    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert led.usage("odds_api")["calls"] == 200


def test_per_game_metrics():
    led = CostLedger(":memory:")
    led.record("odds_api", "odds", units=1, correlation_id="match-401-T-7m", duration_ms=120)
    led.record("claude", "complete", tokens=800, correlation_id="match-401-T-7m", duration_ms=900)
    m = led.metrics_for("match-401-T-7m")
    assert m["calls"] == 2 and m["tokens"] == 800 and m["avg_ms"] > 0
