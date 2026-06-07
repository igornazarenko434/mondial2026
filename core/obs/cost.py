"""Cost & quota ledger.

Persists every external call (provider, endpoint, units/credits, tokens, est $)
to SQLite so you can trace and replay usage, and check it against the free-tier
budgets in config. Always-on and free — your durable record even without an
external APM. Emits metrics too.
"""
from __future__ import annotations
import sqlite3
import threading
import time
from datetime import datetime, timezone
from config import observability as cfg
from core.obs import metrics
from core.obs.logging import get_logger

log = get_logger("obs.cost")

_DDL = """
CREATE TABLE IF NOT EXISTS api_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, provider TEXT, endpoint TEXT,
    units REAL DEFAULT 1, tokens INTEGER DEFAULT 0,
    duration_ms REAL DEFAULT 0,
    est_cost REAL DEFAULT 0, ok INTEGER DEFAULT 1,
    correlation_id TEXT,
    error_class TEXT,
    error_message TEXT
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Backfill columns added after the original schema. ALTER TABLE is a no-op
    if the column already exists (we check via PRAGMA first)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(api_calls)").fetchall()}
    if "error_class" not in cols:
        conn.execute("ALTER TABLE api_calls ADD COLUMN error_class TEXT")
    if "error_message" not in cols:
        conn.execute("ALTER TABLE api_calls ADD COLUMN error_message TEXT")
    # Day-9.11: distinguish 401 vs 429 vs 503 vs Cloudflare-HTML
    if "status_code" not in cols:
        conn.execute("ALTER TABLE api_calls ADD COLUMN status_code INTEGER")
    if "retry_after" not in cols:
        conn.execute("ALTER TABLE api_calls ADD COLUMN retry_after TEXT")
    if "error_kind" not in cols:
        # 'http' / 'timeout' / 'network' / 'ratelimit_timeout' / None
        conn.execute("ALTER TABLE api_calls ADD COLUMN error_kind TEXT")


def _period_start(period: str) -> str:
    now = datetime.now(timezone.utc)
    if period == "day":
        return now.strftime("%Y-%m-%dT00:00:00")
    if period == "month":
        return now.strftime("%Y-%m-01T00:00:00")
    return "1970-01-01T00:00:00"


class CostLedger:
    def __init__(self, db: str | sqlite3.Connection | None = None):
        # check_same_thread=False + a lock -> safe writes from the thread pool.
        if isinstance(db, sqlite3.Connection):
            self.conn = db
        else:
            self.conn = sqlite3.connect(db or ":memory:", check_same_thread=False)
        # RLock so composed methods (quota_status -> usage) don't self-deadlock.
        # Every conn.execute() — read or write — must run under this lock.
        # SQLite with check_same_thread=False lets you share a connection across
        # threads, but doesn't serialize statements: concurrent SELECT during a
        # write drops inserts and can return None from fetchone() on aggregates.
        self._lock = threading.RLock()
        with self._lock:
            self.conn.execute(_DDL)
            _migrate(self.conn)
            self.conn.commit()

    def _est(self, provider: str, units: float, tokens: int) -> float:
        p = cfg.PRICING.get(provider, {})
        return p.get("per_call", 0.0) * units + p.get("per_1k_tokens", 0.0) * (tokens / 1000)

    def record(self, provider: str, endpoint: str, units: float = 1,
               tokens: int = 0, ok: bool = True, correlation_id: str = "-",
               duration_ms: float = 0,
               error_class: str | None = None,
               error_message: str | None = None,
               status_code: int | None = None,
               retry_after: str | None = None,
               error_kind: str | None = None) -> float:
        """Append one row to api_calls. On failure (ok=False) callers SHOULD
        pass error_class (e.g. 'RateLimitError' / 'AuthenticationError' /
        'APITimeoutError'), error_message (~200 chars of the exception repr),
        and where the upstream surfaced them: status_code (401/429/503),
        retry_after (the Retry-After header value, if any), and error_kind
        ('http' / 'timeout' / 'network' / 'ratelimit_timeout'). All optional
        — old call sites that pass only the existing args still work."""
        cost = self._est(provider, units, tokens)
        # Day-9.11: utf-8-safe truncation so a chopped 4-byte codepoint at
        # byte 199 can't poison downstream readers.
        em = ((error_message or "")[:200]
              .encode("utf-8", errors="replace")
              .decode("utf-8", errors="replace")) or None
        # Day-9.11: defense-in-depth — a sick ledger must not replace the
        # real upstream exception. Wrap INSERT in try/except so the caller's
        # exception always survives.
        try:
            with self._lock:
                self.conn.execute(
                    "INSERT INTO api_calls (ts,provider,endpoint,units,tokens,"
                    "duration_ms,est_cost,ok,correlation_id,error_class,"
                    "error_message,status_code,retry_after,error_kind)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (datetime.now(timezone.utc).isoformat(), provider, endpoint,
                     units, tokens, duration_ms, cost, int(ok), correlation_id,
                     error_class, em, status_code, retry_after, error_kind))
                self.conn.commit()
        except sqlite3.Error as e:                     # noqa: BLE001
            log.warning("cost ledger insert failed for %s/%s; dropping row: %s",
                        provider, endpoint, e)
            metrics.incr("cost_ledger_insert_failed", 1, provider=provider)
        metrics.incr("api_calls", 1, provider=provider, endpoint=endpoint)
        if tokens:
            metrics.incr("llm_tokens", tokens, provider=provider)
        try:
            self._maybe_warn(provider)
        except Exception:                              # noqa: BLE001 — best-effort
            pass
        return cost

    def metrics_for(self, correlation_id: str) -> dict:
        """Per-game / per-run metrics straight from the persisted ledger."""
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(tokens),0), COALESCE(AVG(duration_ms),0),"
                " COALESCE(SUM(est_cost),0), SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END)"
                " FROM api_calls WHERE correlation_id=?", (correlation_id,)).fetchone()
        return {"correlation_id": correlation_id, "calls": row[0], "tokens": row[1],
                "avg_ms": round(row[2], 1), "est_cost": round(row[3], 4), "errors": row[4]}

    def usage(self, provider: str, period: str | None = None) -> dict:
        q = "SELECT COUNT(*), COALESCE(SUM(units),0), COALESCE(SUM(tokens),0), COALESCE(SUM(est_cost),0) FROM api_calls WHERE provider=?"
        args = [provider]
        if period:
            q += " AND ts>=?"
            args.append(_period_start(period))
        with self._lock:
            c, units, tokens, cost = self.conn.execute(q, args).fetchone()
        return {"calls": c, "units": units, "tokens": tokens, "est_cost": round(cost, 4)}

    def quota_status(self, provider: str) -> dict:
        lim = cfg.PROVIDER_LIMITS.get(provider, {})
        budget, period = lim.get("budget"), lim.get("budget_period")
        if not budget:
            return {"provider": provider, "budget": None}
        used = self.usage(provider, period)["units"]
        frac = used / budget if budget else 0
        return {"provider": provider, "period": period, "used": used,
                "budget": budget, "fraction": round(frac, 3),
                "warn": frac >= cfg.QUOTA_WARN_FRACTION}

    def over_budget(self, provider: str) -> bool:
        """True if the provider's free-tier budget is exhausted — call BEFORE a
        request so you can skip it (and degrade) instead of getting a hard 429."""
        st = self.quota_status(provider)
        b = st.get("budget")
        return bool(b) and st.get("used", 0) >= b

    def _maybe_warn(self, provider: str):
        st = self.quota_status(provider)
        if st.get("warn"):
            log.warning("quota %.0f%% used for %s (%s/%s this %s)",
                        st["fraction"] * 100, provider, st["used"],
                        st["budget"], st["period"])


_LEDGER: CostLedger | None = None


def ledger() -> CostLedger:
    global _LEDGER
    if _LEDGER is None:
        try:
            _LEDGER = CostLedger(cfg.OBS_DB)
        except Exception:
            _LEDGER = CostLedger(":memory:")
    return _LEDGER
