"""SQLite persistence.

Plain ``sqlite3`` rather than an ORM: the queries are mostly aggregations for the dashboard, which
read better as SQL, and the schema is small enough that a mapping layer would cost more than it
returns.

Nothing here writes a status. Rows record observations and timestamps; ``app.core.state`` derives
everything else. Two conventions are load-bearing and explained on the methods themselves: effects
are recorded *after* they succeed, never reserved beforehand, and the columns that must not go
backwards (``acus``, ``produced_at``, ``closed_at``, ``ever_blocked``) use ``MAX``/``COALESCE``.

The connection is opened per call. At this scale that is cheap and it sidesteps the thread-affinity
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
        # Per-connection, unlike `journal_mode`. Setting it once at startup left every other
        # connection with foreign keys disabled.
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA.read_text(encoding="utf-8"))

    def bump(self, name: str, amount: float = 1) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO counters (name, value) VALUES (?, ?) "
                "ON CONFLICT (name) DO UPDATE SET value = value + excluded.value",
                (name, amount),
            )

    def counters(self) -> dict[str, float]:
        with self._conn() as conn:
            return {r["name"]: r["value"] for r in conn.execute("SELECT name, value FROM counters")}

    # --- effects already performed ------------------------------------------

    def is_done(self, key: str) -> bool:
        with self._conn() as conn:
            return (
                conn.execute("SELECT 1 FROM done_effects WHERE key = ?", (key,)).fetchone()
                is not None
            )

    def mark_done(self, key: str, kind: str) -> None:
        """Record an effect *after* it succeeded.

        Recording after rather than before is what makes this recoverable: a failure never leaves a
        key held, so nothing can be permanently blocked from happening again. The cost is that a
        crash between the API call and this write repeats the effect once, which for a comment or a
        nudge is noise rather than damage.
        """
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO done_effects (key, kind, at) VALUES (?, ?, ?)",
                (key, kind, now()),
            )

    def done_effects(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM done_effects ORDER BY at")]

    # --- deliveries and inbox ----------------------------------------------

    def record_delivery(self, delivery_id: str, event: str, action: str | None) -> bool:
        """Record a delivery id. ``True`` if new. Redeliveries reuse the original GUID."""
        with self._conn() as conn:
            return (
                conn.execute(
                    "INSERT OR IGNORE INTO deliveries (delivery_id, event, action, received_at) "
                    "VALUES (?, ?, ?, ?)",
                    (delivery_id, event, action, now()),
                ).rowcount
                == 1
            )

    def enqueue(self, kind: str, payload: dict[str, Any]) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO inbox (kind, payload, created_at) VALUES (?, ?, ?)",
                (kind, json.dumps(payload), now()),
            )
            return int(cur.lastrowid or 0)

    def pending_inbox(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM inbox WHERE dispatched_at IS NULL ORDER BY id LIMIT ?", (limit,)
            )
            return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def mark_dispatched(self, inbox_id: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE inbox SET dispatched_at = ? WHERE id = ?", (now(), inbox_id))

    def record_inbox_failure(self, inbox_id: int, error: str, *, max_attempts: int) -> bool:
        """Record a failed dispatch. ``True`` once the item is exhausted.

        Retried rather than dropped: forwarded comments have no other source, so discarding one on a
        transient API error loses a human's answer permanently.
        """
        with self._conn() as conn:
            row = conn.execute(
                "UPDATE inbox SET attempts = attempts + 1, last_error = ? WHERE id = ? "
                "RETURNING attempts",
                (error[:500], inbox_id),
            ).fetchone()
            if row is None:
                return False
            if int(row["attempts"]) >= max_attempts:
                conn.execute("UPDATE inbox SET dispatched_at = ? WHERE id = ?", (now(), inbox_id))
                return True
            return False

    # --- issues ------------------------------------------------------------

    def register_issue(self, number: int, title: str, klass: str | None) -> bool:
        """Record an issue as wanted. ``True`` if newly registered.

        ``first_labeled_at`` is written once. It is the MTTR origin, and re-labelling must not reset
        the clock and flatter every duration.
        """
        ts = now()
        with self._conn() as conn:
            return (
                conn.execute(
                    "INSERT OR IGNORE INTO issues "
                    "(number, title, klass, first_labeled_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (number, title, klass, ts, ts),
                ).rowcount
                == 1
            )

    def begin_attempt(self, number: int) -> int:
        """Reserve the next attempt number, before anything billable happens.

        Incrementing first means a failure mid-call can never produce a second session for the same
        attempt, and `last_attempt_at` lets a later retry be recognised even when the call left no
        session row behind.
        """
        ts = now()
        with self._conn() as conn:
            row = conn.execute(
                "UPDATE issues SET attempts = attempts + 1, last_attempt_at = ?, updated_at = ? "
                "WHERE number = ? RETURNING attempts",
                (ts, ts, number),
            ).fetchone()
            return int(row["attempts"]) if row else 0

    def flag_for_human(self, number: int, reason: str) -> bool:
        """Ask for a human. True the first time a given reason applies, so it is said once.

        One flag and one reason is the whole escalation surface. A different reason overwrites the
        first rather than opening a parallel record: an operator needs to know that attention is
        required and what is blocking it *now*.
        """
        ts = now()
        with self._conn() as conn:
            return (
                conn.execute(
                    "UPDATE issues SET needs_human_at = COALESCE(needs_human_at, ?),"
                    " needs_human_reason = ?, updated_at = ?"
                    " WHERE number = ? AND COALESCE(needs_human_reason, '') != ?",
                    (ts, reason, ts, number, reason),
                ).rowcount
                == 1
            )

    def clear_human_flag(self, number: int) -> bool:
        with self._conn() as conn:
            return (
                conn.execute(
                    "UPDATE issues SET needs_human_at = NULL, needs_human_reason = NULL,"
                    " updated_at = ? WHERE number = ? AND needs_human_at IS NOT NULL",
                    (now(), number),
                ).rowcount
                == 1
            )

    def request_retry(self, number: int) -> None:
        """Record that an operator asked for another attempt."""
        ts = now()
        with self._conn() as conn:
            conn.execute(
                "UPDATE issues SET retry_requested_at = ?, updated_at = ? WHERE number = ?",
                (ts, ts, number),
            )

    def issues(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM issues ORDER BY number")]

    def issue(self, number: int) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM issues WHERE number = ?", (number,)).fetchone()
            return dict(row) if row else None

    # --- sessions ----------------------------------------------------------

    def create_session(
        self,
        session_id: str,
        issue_number: int,
        *,
        url: str | None,
        tags: list[str],
        attempt: int,
    ) -> None:
        ts = now()
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sessions "
                "(session_id, issue_number, attempt, url, tags, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, issue_number, attempt, url, json.dumps(tags), ts, ts),
            )

    def record_poll(
        self,
        session_id: str,
        *,
        status: str | None,
        status_detail: str | None,
        acus: float,
        structured_output: Any | None = None,
        blocked: bool = False,
        produced: bool = False,
        closed_reason: str | None = None,
    ) -> bool:
        """Persist a poll result. ``True`` when the status pair changed.

        Four columns are monotonic on purpose:

        ``ever_blocked`` — a session that stopped to ask a question decays into
        ``suspended/inactivity`` once it sleeps, so the observation must be latched or it is lost.

        ``acus`` — takes the maximum. A payload omitting ``acus_consumed`` would otherwise erase
        recorded spend, understating cost and re-opening the global budget. Devin's counter only
        grows, so ``MAX`` is the faithful reading.

        ``produced_at`` and ``closed_at`` — written once each, and independent of one another.
        """
        ts = now()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT status, status_detail FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"no such session: {session_id}")
            changed = (row["status"], row["status_detail"]) != (status, status_detail)
            conn.execute(
                "UPDATE sessions SET status = ?, status_detail = ?, acus = MAX(acus, ?),"
                " updated_at = ?,"
                " ever_blocked = MAX(ever_blocked, ?),"
                " structured_output = COALESCE(?, structured_output),"
                " produced_at = COALESCE(produced_at, ?),"
                " closed_at = COALESCE(closed_at, ?),"
                " closed_reason = COALESCE(closed_reason, ?)"
                " WHERE session_id = ?",
                (
                    status,
                    status_detail,
                    acus,
                    ts,
                    1 if blocked else 0,
                    json.dumps(structured_output) if structured_output is not None else None,
                    ts if produced else None,
                    ts if closed_reason else None,
                    closed_reason,
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

    def close_session(self, session_id: str, reason: str) -> bool:
        """Stop tracking a session, for reasons the status pair does not express."""
        with self._conn() as conn:
            return (
                conn.execute(
                    "UPDATE sessions SET closed_at = ?, closed_reason = ? "
                    "WHERE session_id = ? AND closed_at IS NULL",
                    (now(), reason, session_id),
                ).rowcount
                == 1
            )

    def mark_message_sent(self, session_id: str) -> None:
        """Stamp the grace anchor. Called only by ``Effects`` on a successful send."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE sessions SET last_message_at = ? WHERE session_id = ?", (now(), session_id)
            )

    def bump_nudges(self, session_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE sessions SET nudges = nudges + 1 WHERE session_id = ?", (session_id,)
            )

    def reset_budgets(self, session_id: str) -> None:
        """Grant a fresh nudge and CI budget after a human takes over and hands back.

        Without this the loop re-enters the exhausted branch on the next poll and re-raises the same
        notification the human just answered — the comment-spam loop, on a second path.
        """
        with self._conn() as conn:
            conn.execute("UPDATE sessions SET nudges = 0 WHERE session_id = ?", (session_id,))
            conn.execute(
                "UPDATE pull_requests SET ci_rounds = 0, ci_last_sha = NULL WHERE session_id = ?",
                (session_id,),
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
        """Merge an Analytics row into a session we created. ``False`` for anything else.

        Sparse fields use ``COALESCE`` so a partial row cannot null out values this endpoint is the
        sole source of; ACUs use ``MAX`` so analytics and the per-session read settle on the higher
        figure rather than fighting.
        """
        with self._conn() as conn:
            return (
                conn.execute(
                    "UPDATE sessions SET acus = MAX(acus, ?),"
                    " devin_messages = COALESCE(?, devin_messages),"
                    " user_messages = COALESCE(?, user_messages),"
                    " session_size = COALESCE(?, session_size)"
                    " WHERE session_id = ?",
                    (acus, devin_messages, user_messages, session_size, session_id),
                ).rowcount
                == 1
            )

    def sessions(self, issue_number: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sessions"
        args: tuple[Any, ...] = ()
        if issue_number is not None:
            sql += " WHERE issue_number = ?"
            args = (issue_number,)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY created_at", args)]

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
                    "SELECT * FROM session_events WHERE session_id = ? ORDER BY at", (session_id,)
                )
            ]

    def total_acus(self) -> float:
        with self._conn() as conn:
            return float(
                conn.execute("SELECT COALESCE(SUM(acus), 0) AS t FROM sessions").fetchone()["t"]
            )

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
    ) -> bool:
        with self._conn() as conn:
            return (
                conn.execute(
                    "INSERT OR IGNORE INTO pull_requests "
                    "(pr_number, issue_number, session_id, url, opened_at) VALUES (?, ?, ?, ?, ?)",
                    (pr_number, issue_number, session_id, url, opened_at),
                ).rowcount
                == 1
            )

    def update_pr(self, pr_number: int, **fields: Any) -> None:
        allowed = {
            "merged_at",
            "closed_at",
            "ci_settled_at",
            "ci_conclusion",
            "ci_rounds",
            "ci_last_sha",
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

    def pull_requests(self, issue_number: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM pull_requests"
        args: tuple[Any, ...] = ()
        if issue_number is not None:
            sql += " WHERE issue_number = ?"
            args = (issue_number,)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY pr_number", args)]

    def pull_request(self, pr_number: int) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM pull_requests WHERE pr_number = ?", (pr_number,)
            ).fetchone()
            return dict(row) if row else None

    def tracked_pull_requests(self) -> list[dict[str, Any]]:
        """Pull requests still worth polling: neither merged nor closed."""
        with self._conn() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM pull_requests WHERE merged_at IS NULL AND closed_at IS NULL"
                )
            ]
