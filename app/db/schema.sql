-- Facts, not state.
--
-- Rows record what was observed and when; every status is a pure function of those rows
-- (`app/core/state.py::issue_status`). Nothing stores a status, so nothing can disagree with it.
--
-- **Scope.** This system is watched by a human for the length of a demo, not run unattended for
-- months. Effects are recorded *after* they succeed, so a crash between an API call and its record
-- can repeat that effect once. That is a deliberate trade: the exactly-once machinery this replaced
-- derived its keys from mutable counters and could wedge an issue permanently, which is a far worse
-- failure than a duplicate comment. See the limitations section of the README.
--
-- `issues.first_labeled_at` is the one irreplaceable value: it is the MTTR origin and cannot be
-- recovered from any external API after the fact.

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS issues (
    number             INTEGER PRIMARY KEY,
    title              TEXT,
    klass              TEXT,           -- class:* label, for the variety breakdown
    first_labeled_at   REAL NOT NULL,  -- MTTR origin; never rewritten
    -- Set when an operator re-applies the trigger label. "Try again" is one fact rather than a
    -- state machine that has to be nudged into the right place.
    retry_requested_at REAL,
    -- Incremented *before* the billable call, so a failure mid-flight can never lead to a second
    -- session for the same attempt. `last_attempt_at` lets a retry be recognised even when the
    -- call failed and left no session row.
    attempts           INTEGER NOT NULL DEFAULT 0,
    last_attempt_at    REAL,
    -- The whole escalation surface: one flag and one reason. Set when the loop gives up on making
    -- progress alone, cleared when a human answers or the pull request merges.
    needs_human_at     REAL,
    needs_human_reason TEXT,
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
    last_message_at   REAL,       -- grace anchor, stamped on every outbound message
    -- Two independent facts. Collapsing them meant a session that opened a pull request and went to
    -- sleep counted as ended and its review-fix messages were dropped, while a session that
    -- finished whilst blocked could never report at all.
    produced_at       REAL,       -- delivered its work product; still wakeable
    closed_at         REAL,       -- cannot be revived by any message
    closed_reason     TEXT,
    structured_output TEXT,
    devin_messages    INTEGER,    -- from the Analytics endpoint; nothing else supplies these
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
    ci_last_sha   TEXT,          -- the commit the last round of feedback was sent for
    merged_at     REAL,
    closed_at     REAL
);

-- Effects already performed, recorded after the fact. Keys are built from immutable identifiers
-- only — a comment id, a commit sha, a session id — never from a counter another path can reset.
-- That was the flaw in the version this replaced: a reset counter regenerated a key that was
-- already taken, and the effect could then never happen again.
CREATE TABLE IF NOT EXISTS done_effects (
    key  TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    at   REAL NOT NULL
);

-- Intent recorded by the receiver, drained by the loop. Rows are kept after dispatch: they are the
-- record of what arrived and when it was handled.
CREATE TABLE IF NOT EXISTS inbox (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,
    payload       TEXT NOT NULL,   -- json
    created_at    REAL NOT NULL,
    dispatched_at REAL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT
);

-- Webhook deliveries, deduplicated on two keys.
--
-- The GUID catches GitHub's own redelivery, which reuses it. It cannot catch a replay, because the
-- GUID is a header and only the *body* is signed — an attacker holding one captured (body,
-- signature) pair can resend it with a fresh GUID forever. Since a repeated `issues/labeled` is
-- read as "try again" and starts a paid session, that was a direct route to the ACU budget.
--
-- The body hash is the half that is actually authenticated. Two genuine events never produce
-- byte-identical bodies: every payload carries the full issue object with its timestamps.
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    body_sha    TEXT NOT NULL,
    event       TEXT NOT NULL,
    action      TEXT,
    received_at REAL NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_body ON deliveries (body_sha);

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
