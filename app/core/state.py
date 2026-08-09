"""Interpreting stored facts.

Deliberately pure: no I/O, no clock, no database. The reconcile loop decides what to *do*; this
module decides what things currently *are*. Keeping the two apart is what makes the interesting
logic testable without mocks, and it is why the same function can back both the metrics and the
dashboard — v1 let them diverge because each recomputed parts of a stored status column.

Three ideas carry the weight:

* **Status is derived, never stored.** :func:`issue_status` is the only definition of where an issue
  stands. Nothing writes a status column, so nothing can disagree with it.
* **Liveness is one function.** :func:`liveness` collapses Devin's dozen-odd status pairs into the
  only three answers the orchestrator needs, and it is consulted in exactly one place.
* **Produced and closed are different.** A session that opened a pull request and went to sleep has
  produced its work and is still wakeable. v1 conflated them and silently dropped the review-fix
  messages that the whole design depends on.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

# --- Devin's vocabulary ------------------------------------------------------

STATUS_NEW = "new"
STATUS_CLAIMED = "claimed"
STATUS_RUNNING = "running"
STATUS_EXIT = "exit"
STATUS_ERROR = "error"
STATUS_SUSPENDED = "suspended"
STATUS_RESUMING = "resuming"

DETAIL_WORKING = "working"
DETAIL_WAITING_FOR_USER = "waiting_for_user"
DETAIL_WAITING_FOR_APPROVAL = "waiting_for_approval"
DETAIL_FINISHED = "finished"
DETAIL_INACTIVITY = "inactivity"
DETAIL_USER_REQUEST = "user_request"
DETAIL_ERROR = "error"

#: ``suspended`` details that mean a cost or quota ceiling stopped the work. No message revives a
#: session in one of these, so misclassifying one as sleep means the loop keeps trying to wake
#: something that cannot wake — which is exactly what the cost controls exist to prevent.
COST_HALT_DETAILS = frozenset(
    {
        "usage_limit_exceeded",
        "org_usage_limit_exceeded",
        "user_usage_limit_exceeded",
        "total_session_limit_exceeded",
        "out_of_credits",
        "out_of_quota",
        "no_quota_allocation",
        "payment_declined",
    }
)

#: ``suspended`` details that are ordinary rest. A message wakes these.
SLEEP_DETAILS = frozenset({DETAIL_INACTIVITY, DETAIL_USER_REQUEST})


# --- session liveness --------------------------------------------------------


class Liveness(StrEnum):
    """The only three answers the orchestrator needs about a session."""

    LIVE = "live"
    SLEEPING = "sleeping"
    CLOSED = "closed"


def liveness(session: dict[str, Any]) -> Liveness:
    """Whether a session may be polled and messaged.

    ``closed_at`` is authoritative: once the loop has decided a session cannot be revived, a later
    poll cannot argue otherwise. Otherwise the status pair decides, and anything unrecognised reads
    as ``LIVE`` — the safe direction, because a live session is polled (cheap) rather than abandoned
    (loses work). The age watchdog is what stops an unrecognised status holding a slot forever.
    """
    if session.get("closed_at") is not None:
        return Liveness.CLOSED

    status, detail = session.get("status"), session.get("status_detail")
    if status in (STATUS_EXIT, STATUS_ERROR):
        return Liveness.CLOSED
    if status == STATUS_SUSPENDED:
        if detail in COST_HALT_DETAILS or detail == DETAIL_ERROR:
            return Liveness.CLOSED
        return Liveness.SLEEPING
    return Liveness.LIVE


def closing_reason(session: dict[str, Any]) -> str | None:
    """Why this session can no longer be revived, or ``None`` if it still can."""
    status, detail = session.get("status"), session.get("status_detail")
    if status == STATUS_ERROR:
        return "error"
    if status == STATUS_EXIT:
        return "exit"
    if status == STATUS_SUSPENDED:
        if detail in COST_HALT_DETAILS:
            return f"cost_halt:{detail}"
        if detail == DETAIL_ERROR:
            return "error"
    return None


def is_blocked(session: dict[str, Any]) -> bool:
    """Whether the session is waiting on a human right now."""
    return session.get("status") == STATUS_RUNNING and session.get("status_detail") in (
        DETAIL_WAITING_FOR_USER,
        DETAIL_WAITING_FOR_APPROVAL,
    )


def has_produced(
    session: dict[str, Any], *, has_structured_output: bool, has_pull_request: bool
) -> bool:
    """Whether the session has delivered its work product.

    Distinct from being closed. ``status_detail == 'finished'`` is the explicit signal; a *final*
    structured output is the contractual one; a pull request on a session that has stopped moving is
    the observable one. A pull request on a *working* session is not enough — Devin routinely opens
    one and keeps going.
    """
    if session.get("status_detail") == DETAIL_FINISHED:
        return True
    if has_structured_output:
        return True
    return has_pull_request and liveness(session) in (Liveness.SLEEPING, Liveness.CLOSED)


def is_final_output(structured: Any) -> bool:
    """Whether structured output represents a finished attempt rather than a progress note.

    The schema requires ``outcome``; a session reporting anything outside the terminal set has not
    finished. v1 treated the mere presence of the object as completion and posted "Devin finished"
    on an in-progress session.
    """
    if not isinstance(structured, dict):
        return False
    return structured.get("outcome") in ("fixed", "partially_fixed", "could_not_fix")


# --- issue status ------------------------------------------------------------


class IssueStatus(StrEnum):
    MERGED = "merged"
    AWAITING_HUMAN = "awaiting_human"
    PR_OPEN = "pr_open"
    IN_PROGRESS = "in_progress"
    EXHAUSTED = "exhausted"
    QUEUED = "queued"


#: Statuses from which the loop will not start further work on its own.
TERMINAL_STATUSES = frozenset({IssueStatus.MERGED})


def issue_status(
    issue: dict[str, Any],
    sessions: list[dict[str, Any]],
    pulls: list[dict[str, Any]],
    open_notifications: list[dict[str, Any]],
) -> IssueStatus:
    """Where an issue stands, derived from facts. The single definition, used everywhere.

    Evaluated in precedence order, first match wins. Merged outranks everything because it is an
    outcome observed on GitHub: a session that errors afterwards does not un-merge a pull request.
    """
    if any(p.get("merged_at") for p in pulls):
        return IssueStatus.MERGED
    if open_notifications:
        return IssueStatus.AWAITING_HUMAN
    if any(p.get("opened_at") and not p.get("merged_at") and not p.get("closed_at") for p in pulls):
        return IssueStatus.PR_OPEN
    if any(liveness(s) is not Liveness.CLOSED and s.get("produced_at") is None for s in sessions):
        return IssueStatus.IN_PROGRESS
    if wants_session(issue, sessions):
        return IssueStatus.QUEUED
    return IssueStatus.EXHAUSTED


def wants_session(issue: dict[str, Any], sessions: list[dict[str, Any]]) -> bool:
    """Whether a new session should be started for this issue.

    Two cases: nothing has ever been attempted, or an operator asked for a retry after the newest
    attempt began. Re-applying the trigger label writes ``retry_requested_at``, which is all the
    machinery a retry needs — v1 required a state machine to be nudged into exactly the right place
    and silently did nothing for the most common escalation shape.
    """
    if not sessions:
        return True
    retry_at = issue.get("retry_requested_at")
    if retry_at is None:
        return False
    return retry_at > max(s["created_at"] for s in sessions)


def occupies_slot(session: dict[str, Any]) -> bool:
    """Whether a session counts against the concurrency cap.

    Not closed, full stop. v1 counted only sessions that were *awake*, so blocked and sleeping
    sessions escaped the cap entirely — and both resume spending the moment they are messaged, so
    concurrent spend was unbounded.
    """
    return liveness(session) is not Liveness.CLOSED
