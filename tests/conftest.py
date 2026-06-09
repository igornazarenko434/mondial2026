"""Day-9.22: shared test isolation fixtures.

ROOT-CAUSE FIX (Day-9.22 audit): a handful of scheduler tests
(test_runner_day9.py, test_scheduler.py::test_idempotent_no_double_dispatch,
test_scheduler.py::test_two_simultaneous_matches_run_concurrently) passed
individually but failed during a full-suite run. The investigation found
two cooperating bugs:

  1. `core.obs.runs.runs()` caches a module-level singleton pointing at
     `cfg.OBS_DB` (defaults to `store/obs.db` — the PRODUCTION ledger).
     Any test that records a (match_id, window) pair to that ledger
     persists it forever; the next test that dispatches the same pair
     finds `was_handled() == True` and silently skips.

  2. `core.obs.cost.ledger()` has the same singleton pattern against the
     same SQLite file — quota/budget state from one test was leaking into
     budget-guard checks in another.

The autouse fixture below reseats both singletons against `:memory:` so
every test gets a pristine ledger, regardless of what the persisted file
already contains. Production code paths are untouched (this only runs
inside pytest)."""
from __future__ import annotations
import pytest


@pytest.fixture(autouse=True)
def _isolate_singleton_ledgers(monkeypatch):
    """Point the runs ledger AND cost ledger at fresh in-memory SQLite for
    every test. Prevents cross-test state leakage in CI + local full runs.

    `cfg.OBS_DB` is read at IMPORT time, so monkeypatch.setenv on OBS_DB
    doesn't redirect existing imports — we pre-seed the singletons with
    fresh in-memory instances and clear them at teardown."""
    from core.obs import runs as runs_mod
    from core.obs import cost as cost_mod
    monkeypatch.setattr(runs_mod, "_LEDGER", runs_mod.RunLedger(":memory:"))
    monkeypatch.setattr(cost_mod, "_LEDGER", cost_mod.CostLedger(":memory:"))
    yield
    monkeypatch.setattr(runs_mod, "_LEDGER", None)
    monkeypatch.setattr(cost_mod, "_LEDGER", None)
