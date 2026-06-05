"""Observability layer: rate limiter, cost/quota ledger, tracing no-op."""
from core.obs.ratelimit import TokenBucket
from core.obs.cost import CostLedger
from core.obs.tracing import traced, span


# --- rate limiter (deterministic via injected clock) ---
def test_token_bucket_allows_then_blocks():
    clock = {"t": 0.0}
    b = TokenBucket(rate_per_sec=1.0, capacity=2, now=lambda: clock["t"])
    assert b.try_acquire() is True      # 2 -> 1
    assert b.try_acquire() is True      # 1 -> 0
    assert b.try_acquire() is False     # empty


def test_token_bucket_refills():
    clock = {"t": 0.0}
    b = TokenBucket(rate_per_sec=2.0, capacity=2, now=lambda: clock["t"])
    b.try_acquire(); b.try_acquire()
    assert b.try_acquire() is False
    clock["t"] = 1.0                    # +1s @2/s -> +2 tokens
    assert b.try_acquire() is True


# --- cost ledger ---
def test_ledger_records_and_aggregates():
    led = CostLedger(":memory:")
    led.record("odds_api", "odds", units=2)
    led.record("odds_api", "odds", units=3)
    u = led.usage("odds_api")
    assert u["calls"] == 2 and u["units"] == 5


def test_quota_warn_triggers():
    led = CostLedger(":memory:")
    # odds_api budget is 500/month; push past 80%
    for _ in range(41):
        led.record("odds_api", "odds", units=10)   # 410 units
    st = led.quota_status("odds_api")
    assert st["budget"] == 500 and st["warn"] is True


def test_ledger_estimates_openai_tokens_cost():
    led = CostLedger(":memory:")
    cost = led.record("openai", "complete", tokens=1000)
    assert cost > 0       # openai has a non-zero per_1k_tokens estimate


# --- tracing degrades to no-op without OTel installed ---
def test_traced_runs_without_otel():
    @traced("unit.test")
    def add(a, b):
        return a + b
    assert add(2, 3) == 5
    with span("manual", k=1):
        pass
