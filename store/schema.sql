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

-- Observability: cost/quota ledger (also created by core/obs/cost.py).
CREATE TABLE IF NOT EXISTS api_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, provider TEXT, endpoint TEXT,
    units REAL DEFAULT 1, tokens INTEGER DEFAULT 0,
    duration_ms REAL DEFAULT 0,          -- kept in sync with core/obs/cost.py
    est_cost REAL DEFAULT 0, ok INTEGER DEFAULT 1,
    correlation_id TEXT
);

-- Run-status ledger (also created by core/obs/runs.py): success/failure/fallback
-- per match-window job, so you always know if a run worked or stopped and why.
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT, finished_at TEXT,
    match_id INTEGER, window TEXT,
    status TEXT, fell_back INTEGER DEFAULT 0,
    provider TEXT, attempts INTEGER DEFAULT 1,
    card_delivered INTEGER DEFAULT 0,
    detail TEXT, correlation_id TEXT
);
