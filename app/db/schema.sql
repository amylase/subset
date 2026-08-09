-- Orchestrator state.
--
-- The point of this schema is timestamps more than state: every metric the dashboard reports is
-- derived from transitions recorded here. `issues.labeled_at` in particular is the origin of MTTR
-- and cannot be recovered from any external API after the fact.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Webhook delivery GUIDs. GitHub sends no timestamp header, so storing delivery ids is the only
-- available replay defence, and redeliveries reuse the original GUID.
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    event       TEXT NOT NULL,
    action      TEXT,
    received_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS issues (
    number      INTEGER PRIMARY KEY,
    title       TEXT,
    klass       TEXT,           -- class:* label, for the variety breakdown
    labeled_at  REAL NOT NULL,  -- MTTR origin
    state       TEXT NOT NULL,  -- pending|running|blocked|pr_open|merged|failed|escalated
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id        TEXT PRIMARY KEY,
    issue_number      INTEGER NOT NULL,
    url               TEXT,
    tags              TEXT,       -- json array, mirrors what the Devin dashboard shows
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,
    status            TEXT,
    status_detail     TEXT,
    acus              REAL NOT NULL DEFAULT 0,
    nudges            INTEGER NOT NULL DEFAULT 0,
    ci_rounds         INTEGER NOT NULL DEFAULT 0,
    ever_blocked      INTEGER NOT NULL DEFAULT 0,  -- sticky: waiting_for_user decays into sleep
    finished_at       REAL,
    structured_output TEXT,
    FOREIGN KEY (issue_number) REFERENCES issues (number)
);

-- Full transition log. Kept append-only: the audit trail is part of the deliverable.
CREATE TABLE IF NOT EXISTS session_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    at            REAL NOT NULL,
    status        TEXT,
    status_detail TEXT
);

CREATE TABLE IF NOT EXISTS interventions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT,
    issue_number INTEGER,
    kind         TEXT NOT NULL,  -- auto_nudge|escalation|human_reply|ci_feedback|review_feedback
    at           REAL NOT NULL,
    detail       TEXT
);

CREATE TABLE IF NOT EXISTS pull_requests (
    pr_number     INTEGER PRIMARY KEY,
    issue_number  INTEGER,
    session_id    TEXT,
    url           TEXT,
    opened_at     REAL,
    ci_settled_at REAL,          -- all checks complete: splits CI wait from human review wait
    ci_conclusion TEXT,
    ci_attempts   INTEGER NOT NULL DEFAULT 0,
    merged_at     REAL,
    closed_at     REAL,
    state         TEXT
);

-- Operational counters (dedup hits, retries, cap trips). Cheap, and they make the reliability
-- tier of the dashboard real rather than decorative.
CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value REAL NOT NULL DEFAULT 0
);

-- Intent recorded by the webhook receiver, drained by the reconcile loop.
--
-- The receiver must answer within GitHub's 10 second budget and must not perform side effects, so
-- everything it learns is written here and acted on later. Rows are kept after dispatch rather than
-- deleted: they are the record of what arrived and when it was handled.
CREATE TABLE IF NOT EXISTS queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,
    payload       TEXT NOT NULL,   -- json
    created_at    REAL NOT NULL,
    dispatched_at REAL
);

CREATE INDEX IF NOT EXISTS idx_queue_pending ON queue (dispatched_at, id);
CREATE INDEX IF NOT EXISTS idx_sessions_issue ON sessions (issue_number);
CREATE INDEX IF NOT EXISTS idx_events_session ON session_events (session_id);
CREATE INDEX IF NOT EXISTS idx_pr_issue ON pull_requests (issue_number);
