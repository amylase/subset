"""SQLite persistence.

Plain ``sqlite3`` rather than an ORM: the queries here are mostly aggregations for the dashboard,
which read better as SQL, and the schema is small enough that mapping layers would cost more than
they return.

The connection is opened per call. At this scale that is cheap, and it sidesteps the thread-affinity
rules that bite when a connection is shared between the request path and the background loop.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_SCHEMA = Path(__file__).with_name("schema.sql")


def now() -> float:
    """Wall-clock seconds. Centralised so tests can monkeypatch a single symbol."""
    return time.time()


class Repo:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    # --- plumbing ----------------------------------------------------------

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        # `PRAGMA foreign_keys` is per-connection, not persisted like `journal_mode`. Setting it
        # only in init_schema left every other connection with foreign keys disabled.
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    #: Columns added after the first release. ``CREATE TABLE IF NOT EXISTS`` cannot introduce a
    #: column into a database that already exists, so they are applied additively at startup.
    _MIGRATIONS = (
        ("sessions", "reported_at", "REAL"),
        ("sessions", "last_message_at", "REAL"),
        ("sessions", "closed_at", "REAL"),
        ("sessions", "devin_messages", "INTEGER"),
        ("sessions", "user_messages", "INTEGER"),
        ("sessions", "session_size", "TEXT"),
        ("pull_requests", "ci_feedback_sha", "TEXT"),
        ("queue", "attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("queue", "last_error", "TEXT"),
    )

    def init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
            for table, column, decl in self._MIGRATIONS:
                existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")  # noqa: S608

    def bump(self, name: str, amount: float = 1) -> None:
        """Increment an operational counter."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO counters (name, value) VALUES (?, ?) "
                "ON CONFLICT (name) DO UPDATE SET value = value + excluded.value",
                (name, amount),
            )

    def counters(self) -> dict[str, float]:
        with self._conn() as conn:
            return {r["name"]: r["value"] for r in conn.execute("SELECT name, value FROM counters")}

    # --- deliveries (idempotency) -----------------------------------------

    def record_delivery(self, delivery_id: str, event: str, action: str | None) -> bool:
        """Record a delivery id. Returns ``True`` if it is new, ``False`` if already seen.

        Redelivered webhooks reuse the original GUID, so this catches both GitHub's redelivery
        button and a replayed request.
        """
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO deliveries (delivery_id, event, action, received_at) "
                "VALUES (?, ?, ?, ?)",
                (delivery_id, event, action, now()),
            )
            return cur.rowcount == 1

    # --- queue (intent recorded by the receiver, drained by the loop) -------

    def enqueue(self, kind: str, payload: dict[str, Any]) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO queue (kind, payload, created_at) VALUES (?, ?, ?)",
                (kind, json.dumps(payload), now()),
            )
            return int(cur.lastrowid or 0)

    def pending_queue(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM queue WHERE dispatched_at IS NULL ORDER BY id LIMIT ?", (limit,)
            )
            return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def mark_dispatched(self, queue_id: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE queue SET dispatched_at = ? WHERE id = ?", (now(), queue_id))

    def record_queue_failure(self, queue_id: int, error: str, *, max_attempts: int) -> bool:
        """Record a failed dispatch. Returns ``True`` if the item is now exhausted.

        An item is retried rather than dropped. ``issue_comment`` and ``review_comment`` have no
        other source — no polling pass re-derives them — so discarding one on a transient API
        error loses a human's answer permanently.
        """
        with self._conn() as conn:
            row = conn.execute(
                "UPDATE queue SET attempts = attempts + 1, last_error = ? WHERE id = ? "
                "RETURNING attempts",
                (error[:500], queue_id),
            ).fetchone()
            attempts = int(row["attempts"]) if row else max_attempts
            if attempts >= max_attempts:
                conn.execute("UPDATE queue SET dispatched_at = ? WHERE id = ?", (now(), queue_id))
                return True
            return False

    # --- issues ------------------------------------------------------------

    def upsert_issue(self, number: int, title: str, klass: str | None, labeled_at: float) -> bool:
        """Register an issue as wanted. Returns ``True`` if newly registered.

        ``labeled_at`` is only written on first insert: it is the MTTR origin, and re-labelling must
        not reset the clock and flatter the numbers.
        """
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO issues (number, title, klass, labeled_at, state, updated_at)"
                " VALUES (?, ?, ?, ?, 'pending', ?)",
                (number, title, klass, labeled_at, now()),
            )
            return cur.rowcount == 1

    #: Once an issue reaches one of these, session-derived state must not move it. A merged pull
    #: request is the outcome; a session that later errors or is cancelled does not un-merge it.
    _STICKY_ISSUE_STATES = ("merged",)

    def set_issue_state(self, number: int, state: str, *, force: bool = False) -> bool:
        """Set issue state. Returns ``True`` if it was written.

        Refuses to move an issue out of a sticky terminal state unless ``force`` is given, so a
        late session transition cannot overwrite a recorded outcome and desynchronise the metrics
        from the dashboard.
        """
        with self._conn() as conn:
            placeholders = ",".join("?" * len(self._STICKY_ISSUE_STATES))
            sql = "UPDATE issues SET state = ?, updated_at = ? WHERE number = ?"
            args: tuple[Any, ...] = (state, now(), number)
            if not force:
                sql += f" AND state NOT IN ({placeholders})"  # noqa: S608
                args += self._STICKY_ISSUE_STATES
            return conn.execute(sql, args).rowcount == 1

    def reopen_issue(self, number: int) -> bool:
        """Return a stalled issue to ``pending`` so a new session can be started for it.

        Without this there is no path back: ``upsert_issue`` is ``INSERT OR IGNORE``, so re-applying
        the label to an issue whose session errored is a no-op and the issue is stuck forever.
        """
        with self._conn() as conn:
            return (
                conn.execute(
                    "UPDATE issues SET state = 'pending', updated_at = ? "
                    "WHERE number = ? AND state IN ('failed', 'escalated')",
                    (now(), number),
                ).rowcount
                == 1
            )

    def issues(self, state: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM issues"
        args: tuple[Any, ...] = ()
        if state:
            sql += " WHERE state = ?"
            args = (state,)
        sql += " ORDER BY number"
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, args)]

    def issue(self, number: int) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM issues WHERE number = ?", (number,)).fetchone()
            return dict(row) if row else None

    # --- sessions ----------------------------------------------------------

    def create_session(
        self, session_id: str, issue_number: int, url: str | None, tags: list[str]
    ) -> None:
        ts = now()
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sessions "
                "(session_id, issue_number, url, tags, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, issue_number, url, json.dumps(tags), ts, ts),
            )

    def update_session(
        self,
        session_id: str,
        *,
        status: str | None,
        status_detail: str | None,
        acus: float,
        structured_output: Any | None = None,
        blocked: bool = False,
        finished: bool = False,
        closed: bool = False,
    ) -> bool:
        """Persist a poll result. Returns ``True`` when the status pair changed.

        Three fields are deliberately monotonic:

        ``ever_blocked`` — a session that stopped to ask a question decays into
        ``suspended/inactivity`` once it sleeps, so the blocked observation must be latched or it is
        lost between polls.

        ``finished_at`` — written once, when the session has produced its work product.

        ``closed_at`` — written once, when the session can no longer be revived at all. Kept
        separate: a session that opened a pull request and went to sleep is finished but still
        wakeable, and the review-fix loop depends on being able to message it.

        ``acus`` — takes the maximum rather than the last value. A poll whose payload omits
        ``acus_consumed`` would otherwise erase recorded spend, understating cost and re-opening the
        global budget. Devin's ACU counter only ever grows, so ``MAX`` is the faithful reading.
        """
        ts = now()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT status, status_detail, finished_at FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"no such session: {session_id}")
            changed = (row["status"], row["status_detail"]) != (status, status_detail)
            conn.execute(
                "UPDATE sessions SET status = ?, status_detail = ?, acus = MAX(acus, ?),"
                " updated_at = ?,"
                " ever_blocked = MAX(ever_blocked, ?),"
                " structured_output = COALESCE(?, structured_output),"
                " finished_at = COALESCE(finished_at, ?),"
                " closed_at = COALESCE(closed_at, ?)"
                " WHERE session_id = ?",
                (
                    status,
                    status_detail,
                    acus,
                    ts,
                    1 if blocked else 0,
                    json.dumps(structured_output) if structured_output is not None else None,
                    ts if finished else None,
                    ts if closed else None,
                    session_id,
                ),
            )
            if changed:
                conn.execute(
                    "INSERT INTO session_events (session_id, at, status, status_detail) "
                    "VALUES (?, ?, ?, ?)",
                    (session_id, ts, status, status_detail),
                )
            return changed

    def bump_session(self, session_id: str, column: str) -> None:
        """Increment ``nudges`` or ``ci_rounds``."""
        if column not in {"nudges", "ci_rounds"}:
            raise ValueError(f"not a counter column: {column}")
        with self._conn() as conn:
            conn.execute(
                f"UPDATE sessions SET {column} = {column} + 1 WHERE session_id = ?",  # noqa: S608
                (session_id,),
            )

    def mark_message_sent(self, session_id: str) -> None:
        """Stamp the grace-period anchor after sending anything to a session.

        The loop must not nudge or escalate a session that has not yet had a chance to act on the
        last message. Without this, forwarding a human's answer is immediately followed — in the
        same tick — by another escalation, because the session still reads ``waiting_for_user``.
        """
        with self._conn() as conn:
            conn.execute(
                "UPDATE sessions SET last_message_at = ? WHERE session_id = ?", (now(), session_id)
            )

    def apply_insight(
        self,
        session_id: str,
        *,
        acus: float,
        devin_messages: int | None,
        user_messages: int | None,
        session_size: str | None,
    ) -> bool:
        """Merge a row from the Analytics endpoint into a session we already know about.

        ACUs go into the same ``acus`` column under ``MAX``, so the analytics figure and the
        per-session read reconcile to the higher of the two rather than fighting each other. The
        message counts and size classification exist only here.

        Returns ``False`` for a session id we did not create — the org may contain sessions from
        other sources, and only our own belong in these metrics.
        """
        with self._conn() as conn:
            return (
                conn.execute(
                    "UPDATE sessions SET acus = MAX(acus, ?), devin_messages = ?,"
                    " user_messages = ?, session_size = ? WHERE session_id = ?",
                    (acus, devin_messages, user_messages, session_size, session_id),
                ).rowcount
                == 1
            )

    def close_session(self, session_id: str) -> bool:
        """Stop tracking a session. Used by the watchdog for what the phases do not cover."""
        with self._conn() as conn:
            return (
                conn.execute(
                    "UPDATE sessions SET closed_at = ? WHERE session_id = ? AND closed_at IS NULL",
                    (now(), session_id),
                ).rowcount
                == 1
            )

    def clear_nudges(self, session_id: str) -> None:
        """Reset the nudge budget, used when a human takes over and hands back."""
        with self._conn() as conn:
            conn.execute("UPDATE sessions SET nudges = 0 WHERE session_id = ?", (session_id,))

    def mark_reported(self, session_id: str) -> bool:
        """Claim the right to write the completion comment. ``True`` for the first caller only."""
        with self._conn() as conn:
            return (
                conn.execute(
                    "UPDATE sessions SET reported_at = ? "
                    "WHERE session_id = ? AND reported_at IS NULL",
                    (now(), session_id),
                ).rowcount
                == 1
            )

    def sessions(self, issue_number: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sessions"
        args: tuple[Any, ...] = ()
        if issue_number is not None:
            sql += " WHERE issue_number = ?"
            args = (issue_number,)
        sql += " ORDER BY created_at"
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, args)]

    def session(self, session_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            return dict(row) if row else None

    def latest_session_for_issue(self, issue_number: int) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE issue_number = ? ORDER BY created_at DESC LIMIT 1",
                (issue_number,),
            ).fetchone()
            return dict(row) if row else None

    def session_events(self, session_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM session_events WHERE session_id = ? ORDER BY at",
                    (session_id,),
                )
            ]

    # --- interventions -----------------------------------------------------

    def record_intervention(
        self, kind: str, *, session_id: str | None, issue_number: int | None, detail: str = ""
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO interventions (session_id, issue_number, kind, at, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, issue_number, kind, now(), detail[:2000]),
            )

    def interventions(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM interventions ORDER BY at")]

    # --- pull requests -----------------------------------------------------

    def upsert_pr(
        self,
        pr_number: int,
        *,
        issue_number: int | None,
        session_id: str | None,
        url: str | None,
        opened_at: float | None,
        state: str,
    ) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO pull_requests "
                "(pr_number, issue_number, session_id, url, opened_at, state) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (pr_number, issue_number, session_id, url, opened_at, state),
            )
            return cur.rowcount == 1

    def update_pr(self, pr_number: int, **fields: Any) -> None:
        allowed = {
            "state",
            "merged_at",
            "closed_at",
            "ci_settled_at",
            "ci_conclusion",
            "ci_attempts",
            "ci_feedback_sha",
            "issue_number",
            "session_id",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"not updatable: {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE pull_requests SET {assignments} WHERE pr_number = ?",  # noqa: S608
                (*fields.values(), pr_number),
            )

    def pull_requests(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM pull_requests ORDER BY pr_number")]

    def open_pull_requests(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM pull_requests WHERE merged_at IS NULL AND closed_at IS NULL"
                )
            ]

    def pull_request(self, pr_number: int) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM pull_requests WHERE pr_number = ?", (pr_number,)
            ).fetchone()
            return dict(row) if row else None

    def pr_for_issue(self, issue_number: int) -> dict[str, Any] | None:
        """The pull request that represents this issue's outcome.

        A merged one wins over a newer unmerged one. Devin can open more than one pull request for
        an issue, and taking simply the highest number reported the issue as unresolved even though
        an earlier pull request had merged.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM pull_requests WHERE issue_number = ?"
                " ORDER BY (merged_at IS NOT NULL) DESC, pr_number DESC LIMIT 1",
                (issue_number,),
            ).fetchone()
            return dict(row) if row else None

    def total_acus(self) -> float:
        with self._conn() as conn:
            row = conn.execute("SELECT COALESCE(SUM(acus), 0) AS t FROM sessions").fetchone()
            return float(row["t"])
