"""Mapping Devin session state onto orchestrator state.

This module is deliberately pure: no I/O, no clock, no database. The reconcile loop decides what to
*do*; this module decides what a session currently *is*. Keeping the two apart is what makes the
interesting logic testable without mocks.

The subtlety worth knowing before editing:

* The API sample loop in Devin's docs breaks on ``status in ('exit', 'error', 'suspended')``. That
  is wrong in both directions. A session whose task is finished can still report ``running`` with
  ``status_detail == 'finished'``, and ``suspended`` is usually just sleep, not an ending.
* Sessions sleep automatically after ~0.1 ACU of inactivity, so a session that stopped to ask a
  question decays from ``running/waiting_for_user`` into ``suspended/inactivity``. The blocked state
  is therefore transient and must be latched when first observed, not inferred later.
"""

from __future__ import annotations

from enum import StrEnum

# --- Devin vocabulary --------------------------------------------------------

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

#: ``suspended`` details that mean a cost or quota ceiling stopped the work. These are the only
#: suspensions that are genuinely bad news; everything else is sleep.
#:
#: The full documented set matters here. A halt this list misses is classified as sleep, so the
#: orchestrator keeps sending wake-up messages that cannot succeed, never escalates, and shows the
#: issue as running — the exact failure the cost controls exist to prevent.
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


class Phase(StrEnum):
    """Where a session is in its lifecycle."""

    STARTING = "starting"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETE = "complete"
    SLEEPING = "sleeping"
    HALTED_COST = "halted_cost"
    RESUMING = "resuming"
    ENDED = "ended"
    FAILED = "failed"


#: Phases from which a session cannot come back on its own.
TERMINAL_PHASES = frozenset({Phase.ENDED, Phase.FAILED})

#: Phases after which polling should stop. ``HALTED_COST`` is included because a quota or credit
#: ceiling will not lift on its own: the orchestrator has already escalated, and continuing to poll
#: only burns API calls and inflates counters.
CLOSED_PHASES = frozenset({Phase.ENDED, Phase.FAILED, Phase.HALTED_COST})

#: Phases in which the session is awake and spending ACUs.
ACTIVE_PHASES = frozenset({Phase.STARTING, Phase.IN_PROGRESS, Phase.RESUMING})

#: Phases that a message can revive. Devin resumes a suspended session on receiving one.
WAKEABLE_PHASES = frozenset({Phase.SLEEPING, Phase.BLOCKED, Phase.COMPLETE})


def classify(status: str | None, status_detail: str | None) -> Phase:
    """Map a Devin ``(status, status_detail)`` pair onto a :class:`Phase`.

    Unknown values degrade to the closest safe interpretation rather than raising: the API may grow
    new detail values, and an unrecognised suspension should read as sleep (recoverable), not as
    failure (which would corrupt the success rate).
    """
    match status:
        case None:
            return Phase.STARTING
        case s if s in (STATUS_NEW, STATUS_CLAIMED):
            return Phase.STARTING
        case s if s == STATUS_RESUMING:
            return Phase.RESUMING
        case s if s == STATUS_EXIT:
            return Phase.ENDED
        case s if s == STATUS_ERROR:
            return Phase.FAILED
        case s if s == STATUS_RUNNING:
            if status_detail == DETAIL_FINISHED:
                return Phase.COMPLETE
            if status_detail in (DETAIL_WAITING_FOR_USER, DETAIL_WAITING_FOR_APPROVAL):
                return Phase.BLOCKED
            return Phase.IN_PROGRESS
        case s if s == STATUS_SUSPENDED:
            if status_detail in COST_HALT_DETAILS:
                return Phase.HALTED_COST
            if status_detail == DETAIL_ERROR:
                # `suspended/error` is a documented pair and is not sleep: no message will revive
                # it, so treating it as wakeable would poll and nudge a dead session forever.
                return Phase.FAILED
            return Phase.SLEEPING
        case _:
            return Phase.IN_PROGRESS


def is_work_done(
    phase: Phase,
    *,
    has_structured_output: bool,
    has_pull_request: bool,
) -> bool:
    """Whether the session has produced its work product.

    Distinct from the phase on purpose. ``COMPLETE`` is the explicit signal, structured output is
    the contractual one (the schema is required, so its presence means Devin called
    ``provide_structured_output`` with ``is_final=true``), and a pull request on a session that has
    stopped moving is the observable one.

    A pull request on a *running* session is not enough: Devin routinely opens a PR and keeps
    working on it.
    """
    if phase is Phase.COMPLETE:
        return True
    if has_structured_output:
        return True
    return has_pull_request and phase in (Phase.SLEEPING, Phase.ENDED)


def needs_attention(phase: Phase) -> bool:
    """Phases a human should eventually hear about if they persist."""
    return phase in (Phase.BLOCKED, Phase.HALTED_COST, Phase.FAILED)


class IssueState(StrEnum):
    """Issue-level state. This is the denominator the dashboard reports against.

    Metrics are counted per issue, not per session: an issue that needed three sessions and got
    fixed is one success, not one success and two failures.
    """

    PENDING = "pending"
    RUNNING = "running"
    BLOCKED = "blocked"
    PR_OPEN = "pr_open"
    MERGED = "merged"
    ESCALATED = "escalated"
    FAILED = "failed"


#: Issue states from which no further automated work will happen.
ISSUE_TERMINAL = frozenset({IssueState.MERGED, IssueState.FAILED})


def issue_state_for(
    phase: Phase | None,
    *,
    pr_merged: bool,
    pr_open: bool,
    escalated: bool,
    has_session: bool = True,
) -> IssueState:
    """Derive the issue-level state. Outcomes observed on GitHub outrank session phase.

    ``has_session`` distinguishes "no session yet" from "a session that has not started moving".
    Without it, an issue held back by the concurrency cap displayed as in progress, which is
    precisely the wrong reading — it is queued, and the queue depth is what an operator needs to
    see when work is not flowing.
    """
    if pr_merged:
        return IssueState.MERGED
    if escalated:
        return IssueState.ESCALATED
    if pr_open:
        return IssueState.PR_OPEN
    if not has_session:
        return IssueState.PENDING
    if phase is Phase.FAILED:
        return IssueState.FAILED
    if phase in (Phase.BLOCKED, Phase.HALTED_COST):
        return IssueState.BLOCKED
    return IssueState.RUNNING
