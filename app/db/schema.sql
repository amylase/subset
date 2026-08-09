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
    -- Set once the session can no longer make progress on its own (done, ended, errored, or
    -- halted on cost). Polling stops here; without it a dead session is polled forever.
    finished_at       REAL,
    -- Set when the session can no longer be revived at all: ended, errored, or halted on a cost
    -- ceiling. Distinct from finished_at, which only means "produced its work product" -- a session
    -- that opened a pull request and went to sleep is finished but very much revivable, and the
    -- review-fix loop depends on still being able to message it.
    closed_at         REAL,
    -- Separate from finished_at on purpose. Overloading one column meant a session that finished
    -- while blocked latched finished_at during the nudge branch and could then never report.
    reported_at       REAL,
    -- Grace period anchor. Any outbound message stamps this, so the loop does not nudge or
    -- escalate a session that has not yet had a chance to act on the last thing it was sent.
    last_message_at   REAL,
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
    -- The commit a CI failure was last handed back for. Without this the feedback budget is spent
    -- re-reporting the same red commit on consecutive polls, before Devin can push anything.
    ci_feedback_sha TEXT,
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
    dispatched_at REAL,
    -- Retry counter. A transient API error must not destroy intent: issue_comment and
    -- review_comment have no other source, so a dropped one is a human's answer lost for good.
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT
);

CREATE INDEX IF NOT EXISTS idx_queue_pending ON queue (dispatched_at, id);
CREATE INDEX IF NOT EXISTS idx_sessions_issue ON sessions (issue_number);
CREATE INDEX IF NOT EXISTS idx_events_session ON session_events (session_id);
CREATE INDEX IF NOT EXISTS idx_pr_issue ON pull_requests (issue_number);
