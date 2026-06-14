-- SQLite schema: the single source of truth.
CREATE TABLE IF NOT EXISTS matches (
    match_id      INTEGER PRIMARY KEY,
    utc_kickoff   TEXT,
    local_kickoff TEXT,
    stage         TEXT,        -- RULES stage stored by ingest: Group/R32/R16/QF/SF/3rd/Final
    grp           TEXT,
    home          TEXT,
    away          TEXT,
    status        TEXT,        -- SCHEDULED/TIMED/IN_PLAY/FINISHED
    home_goals    INTEGER,
    away_goals    INTEGER,
    detonator     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS odds_snapshots (
    match_id   INTEGER,
    captured_at TEXT,          -- 'T-60m' / 'T-15m' / 'T-7m(lock)'
    book       TEXT,           -- pinnacle / betfair / consensus
    odds_h     REAL, odds_d REAL, odds_a REAL,
    PRIMARY KEY (match_id, captured_at, book)
);

CREATE TABLE IF NOT EXISTS predictions (
    match_id    INTEGER,
    created_at  TEXT,
    window      TEXT,          -- which time window produced it
    pick_dir    TEXT,          -- H/D/A
    pick_h      INTEGER, pick_a INTEGER,
    modal_h     INTEGER, modal_a INTEGER,
    expected_points REAL,
    payload_json TEXT,         -- full recommendation card
    PRIMARY KEY (match_id, window)
);

CREATE TABLE IF NOT EXISTS standings (
    participant TEXT PRIMARY KEY,
    group_points REAL DEFAULT 0,
    knockout_points REAL DEFAULT 0,
    futures_points REAL DEFAULT 0,
    side_points REAL DEFAULT 0     -- Day-9.26: Negev's Side Bets column
);

-- Day-9.26: track which side-bet shells we've already alerted on.
-- The Negev app marks shells as isResolved=true + correctAnswer=Yes/No when
-- a side bet resolves. We poll on every sync tick; when we see a new
-- resolution we send a Telegram alert with ready-to-paste CLI commands
-- for the operator (since per-user side-bet picks are at an admin-only
-- Firestore path our auth cannot read).
CREATE TABLE IF NOT EXISTS side_bet_state (
    side_bet_id     TEXT PRIMARY KEY,
    tournament_id   TEXT NOT NULL,
    question        TEXT,
    correct_answer  TEXT,
    is_resolved     INTEGER DEFAULT 0,
    notified_at     TEXT,
    seen_at         TEXT
);

-- Observability: cost/quota ledger (also created/migrated by core/obs/cost.py).
-- api_calls and runs are in the same DB as game data (mondial.db) so all
-- diagnostic queries can join predictions ↔ api_calls ↔ runs without ATTACH.
-- Each LLM call produces TWO rows: units=1 (the call) + units=0 (token update).
-- Filter WHERE units > 0 when counting actual calls by provider.
CREATE TABLE IF NOT EXISTS api_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, provider TEXT, endpoint TEXT,
    units REAL DEFAULT 1, tokens INTEGER DEFAULT 0,
    duration_ms REAL DEFAULT 0,
    est_cost REAL DEFAULT 0, ok INTEGER DEFAULT 1,
    correlation_id TEXT,
    error_class TEXT,       -- type(e).__name__ on failure
    error_message TEXT,     -- first 200 chars of str(e)
    status_code INTEGER,    -- HTTP status if available (401/429/503)
    retry_after TEXT,       -- Retry-After header value if any
    error_kind TEXT         -- 'http'/'timeout'/'network'/'ratelimit_timeout'/'other'
);

-- Run-status ledger (also created by core/obs/runs.py): success/failure/fallback
-- per match-window job, so you always know if a run worked or stopped and why.
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT, finished_at TEXT,
    match_id INTEGER, window TEXT,
    status TEXT,            -- started | ok | failed
    fell_back INTEGER DEFAULT 0,
    provider TEXT,          -- LLM provider that actually answered (news_provider)
    attempts INTEGER DEFAULT 1,
    card_delivered INTEGER DEFAULT 0,
    detail TEXT,            -- error message / note
    correlation_id TEXT
);
