-- Facts, not state.
--
-- No table here stores a status. Rows record what was observed and when; every status is a pure
-- function of those rows (see `app/core/state.py`). v1 stored `issues.state`, wrote it from seven
-- call sites, and had the metrics and the dashboard each recompute parts of it differently — which
-- produced a session error overwriting a merged outcome, two dashboard panels disagreeing about the
-- same issue, and an escalation guard that collapsed whenever another path reset the column.
--
-- `issues.first_labeled_at` is the one irreplaceable value: it is the MTTR origin and cannot be
-- recovered from any external API after the fact.

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS issues (
    number             INTEGER PRIMARY KEY,
    title              TEXT,
    klass              TEXT,           -- class:* label, for the variety breakdown
    first_labeled_at   REAL NOT NULL,  -- MTTR origin; never rewritten
    -- Set when an operator re-applies the trigger label. "Try again" is then one fact rather than a
    -- state machine that has to be nudged into the right place.
    retry_requested_at REAL,
    updated_at         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id        TEXT PRIMARY KEY,
    issue_number      INTEGER NOT NULL,
    attempt           INTEGER NOT NULL DEFAULT 1,
    url               TEXT,
    tags              TEXT,       -- json array, mirrors what the Devin dashboard shows
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,
    status            TEXT,
    status_detail     TEXT,
    acus              REAL NOT NULL DEFAULT 0,
    nudges            INTEGER NOT NULL DEFAULT 0,
    ever_blocked      INTEGER NOT NULL DEFAULT 0,  -- sticky: waiting_for_user decays into sleep
    last_message_at   REAL,       -- grace anchor: stamped by Effects on every outbound message
    -- Two independent facts. v1 collapsed them into one column, so a session that opened a pull
    -- request and went to sleep counted as ended and its review-fix messages were silently dropped,
    -- while a session that finished whilst blocked could never report at all.
    produced_at       REAL,       -- delivered its work product; still wakeable
    closed_at         REAL,       -- cannot be revived by any message
    closed_reason     TEXT,
    structured_output TEXT,
    -- From the Analytics endpoint; nothing else supplies these.
    devin_messages    INTEGER,
    user_messages     INTEGER,
    session_size      TEXT,
    FOREIGN KEY (issue_number) REFERENCES issues (number)
);

-- Append-only transition log. The audit trail is part of the deliverable.
CREATE TABLE IF NOT EXISTS session_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    at            REAL NOT NULL,
    status        TEXT,
    status_detail TEXT
);

CREATE TABLE IF NOT EXISTS pull_requests (
    pr_number     INTEGER PRIMARY KEY,
    issue_number  INTEGER,
    session_id    TEXT,
    url           TEXT,
    opened_at     REAL,
    ci_settled_at REAL,          -- all checks complete: splits CI wait from human review wait
    ci_conclusion TEXT,
    ci_rounds     INTEGER NOT NULL DEFAULT 0,
    merged_at     REAL,
    closed_at     REAL
);

-- Open notifications are what `awaiting human` is derived from, and they are the honesty surface:
-- every bound, stall and failure opens one. Keyed by reason class, so a cost halt after a question
-- escalation is reported rather than swallowed — v1 deduped on issue state and lost the second,
-- different reason.
CREATE TABLE IF NOT EXISTS notifications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_number INTEGER NOT NULL,
    reason_class TEXT NOT NULL,
    session_id   TEXT,
    detail       TEXT,
    opened_at    REAL NOT NULL,
    resolved_at  REAL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_notification_open
    ON notifications (issue_number, reason_class) WHERE resolved_at IS NULL;

-- The idempotency ledger. Every outward action claims a natural key here before it happens and
-- confirms afterwards; a failure that provably did nothing releases the key. v1 invented a separate
-- guard per effect and each one had a hole.
CREATE TABLE IF NOT EXISTS effects (
    key        TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    claimed_at REAL NOT NULL,
    done_at    REAL
);

-- Intent recorded by the receiver, drained by the loop. Rows are kept after dispatch: they are the
-- record of what arrived and when it was handled.
CREATE TABLE IF NOT EXISTS inbox (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,
    payload       TEXT NOT NULL,   -- json
    provenance    TEXT NOT NULL,   -- assigned at ingest, never re-derived
    created_at    REAL NOT NULL,
    dispatched_at REAL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT
);

-- Webhook delivery GUIDs. GitHub sends no timestamp header, so this is the only replay defence
-- available; note the GUID is outside the signed body (see the limitations in the README).
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    event       TEXT NOT NULL,
    action      TEXT,
    received_at REAL NOT NULL
);

-- Anything a human or the system did to help a session along. Drives the autonomy rate.
CREATE TABLE IF NOT EXISTS interventions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT,
    issue_number INTEGER,
    kind         TEXT NOT NULL,
    at           REAL NOT NULL,
    detail       TEXT
);

CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_inbox_pending ON inbox (dispatched_at, id);
CREATE INDEX IF NOT EXISTS idx_sessions_issue ON sessions (issue_number);
CREATE INDEX IF NOT EXISTS idx_events_session ON session_events (session_id);
CREATE INDEX IF NOT EXISTS idx_pr_issue ON pull_requests (issue_number);
CREATE INDEX IF NOT EXISTS idx_notifications_issue ON notifications (issue_number);
