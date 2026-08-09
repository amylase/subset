"""Interpreting stored facts.

These are the definitions the rest of the system agrees on, so they are pinned exhaustively. The
cases that matter: a finished task still reports ``running``; a suspended session is usually asleep
rather than dead; and produced and closed are independent, because collapsing them in v1 both
dropped review-fix messages and polled dead sessions forever.
"""

from __future__ import annotations

import pytest

from app.core.state import (
    IssueStatus,
    Liveness,
    closing_reason,
    has_produced,
    is_blocked,
    is_final_output,
    issue_status,
    liveness,
    occupies_slot,
    wants_session,
)


def session(status="running", detail="working", **extra):
    return {
        "session_id": "s1",
        "issue_number": 2,
        "status": status,
        "status_detail": detail,
        "created_at": 0.0,
        "produced_at": None,
        "closed_at": None,
        "nudges": 0,
        "last_message_at": None,
        **extra,
    }


def pull(**extra):
    base = {
        "pr_number": 10,
        "issue_number": 2,
        "session_id": "s1",
        "opened_at": 1.0,
        "merged_at": None,
        "closed_at": None,
    }
    return {**base, **extra}


# --- liveness ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "detail", "expected"),
    [
        ("new", None, Liveness.LIVE),
        ("claimed", None, Liveness.LIVE),
        ("running", "working", Liveness.LIVE),
        ("running", "waiting_for_user", Liveness.LIVE),
        ("running", "finished", Liveness.LIVE),
        ("resuming", None, Liveness.LIVE),
        ("suspended", "inactivity", Liveness.SLEEPING),
        ("suspended", "user_request", Liveness.SLEEPING),
        ("suspended", "out_of_credits", Liveness.CLOSED),
        ("suspended", "no_quota_allocation", Liveness.CLOSED),
        ("suspended", "user_usage_limit_exceeded", Liveness.CLOSED),
        ("suspended", "total_session_limit_exceeded", Liveness.CLOSED),
        ("suspended", "error", Liveness.CLOSED),
        ("exit", None, Liveness.CLOSED),
        ("error", None, Liveness.CLOSED),
    ],
)
def test_liveness(status, detail, expected):
    assert liveness(session(status, detail)) is expected


def test_a_recorded_closure_outranks_a_later_poll():
    """Once the loop decides a session is unrevivable, a stale poll cannot argue otherwise."""
    assert liveness(session("running", "working", closed_at=1.0)) is Liveness.CLOSED


def test_an_unknown_status_stays_live():
    """Live is the safe direction: polled cheaply, not abandoned. The watchdog bounds it."""
    assert liveness(session("teleporting", None)) is Liveness.LIVE


@pytest.mark.parametrize(
    ("status", "detail", "expected"),
    [
        ("error", None, "error"),
        ("exit", None, "exit"),
        ("suspended", "out_of_credits", "cost_halt:out_of_credits"),
        ("suspended", "error", "error"),
        ("suspended", "inactivity", None),
        ("running", "working", None),
    ],
)
def test_closing_reason(status, detail, expected):
    assert closing_reason(session(status, detail)) == expected


def test_a_sleeping_session_still_occupies_a_slot():
    """v1 counted only awake sessions, so blocked and sleeping ones escaped the cap entirely —
    and both resume spending the moment they are messaged."""
    assert occupies_slot(session("suspended", "inactivity"))
    assert occupies_slot(session("running", "waiting_for_user"))
    assert not occupies_slot(session("exit", None))


# --- produced ---------------------------------------------------------------


def test_finished_means_produced():
    assert has_produced(
        session("running", "finished"), has_structured_output=False, has_pull_request=False
    )


def test_a_pr_on_a_sleeping_session_counts_as_produced():
    assert has_produced(
        session("suspended", "inactivity"), has_structured_output=False, has_pull_request=True
    )


def test_a_pr_on_a_working_session_does_not():
    """Devin routinely opens a pull request and keeps working on it."""
    assert not has_produced(
        session("running", "working"), has_structured_output=False, has_pull_request=True
    )


@pytest.mark.parametrize("outcome", ["fixed", "partially_fixed", "could_not_fix"])
def test_terminal_outcomes_are_final(outcome):
    assert is_final_output({"outcome": outcome})


@pytest.mark.parametrize("structured", [None, {}, {"outcome": "in_progress"}, "text", []])
def test_non_terminal_output_is_not_final(structured):
    """v1 treated the mere presence of the object as completion and announced 'Devin finished'
    on a session that was still working."""
    assert not is_final_output(structured)


def test_progress_output_does_not_mark_a_session_produced():
    assert not has_produced(
        session("running", "working"),
        has_structured_output=is_final_output({"outcome": "in_progress"}),
        has_pull_request=False,
    )


@pytest.mark.parametrize(
    ("detail", "expected"),
    [("waiting_for_user", True), ("waiting_for_approval", True), ("working", False)],
)
def test_is_blocked(detail, expected):
    assert is_blocked(session("running", detail)) is expected


# --- issue status -----------------------------------------------------------


def issue(**extra):
    return {"number": 2, "first_labeled_at": 0.0, "retry_requested_at": None, **extra}


def test_merged_outranks_everything():
    status = issue_status(
        issue(),
        [session("error", None, closed_at=5.0)],
        [pull(merged_at=9.0)],
        [{"reason_class": "blocked_on_question"}],
    )
    assert status is IssueStatus.MERGED


def test_an_open_notification_outranks_an_open_pr():
    status = issue_status(issue(), [session()], [pull()], [{"reason_class": "ci_unresolved"}])
    assert status is IssueStatus.AWAITING_HUMAN


def test_an_open_pr_outranks_a_running_session():
    assert issue_status(issue(), [session()], [pull()], []) is IssueStatus.PR_OPEN


def test_a_live_session_is_in_progress():
    assert issue_status(issue(), [session()], [], []) is IssueStatus.IN_PROGRESS


def test_no_session_is_queued():
    assert issue_status(issue(), [], [], []) is IssueStatus.QUEUED


def test_all_sessions_closed_is_exhausted():
    status = issue_status(issue(), [session("error", None, closed_at=5.0)], [], [])
    assert status is IssueStatus.EXHAUSTED


def test_a_closed_pr_does_not_keep_an_issue_open():
    status = issue_status(
        issue(), [session("exit", None, closed_at=5.0)], [pull(closed_at=8.0)], []
    )
    assert status is IssueStatus.EXHAUSTED


def test_a_produced_session_is_not_in_progress():
    """Otherwise a session that opened a pull request and rested would read as still working."""
    status = issue_status(issue(), [session("suspended", "inactivity", produced_at=4.0)], [], [])
    assert status is IssueStatus.EXHAUSTED


# --- retries ----------------------------------------------------------------


def test_a_retry_after_the_newest_attempt_queues_again():
    """The documented contract: re-apply the label to try again. v1 needed a state machine to be
    nudged into exactly the right place and did nothing for the commonest escalation shape."""
    sessions = [session("error", None, closed_at=5.0, created_at=1.0)]
    assert wants_session(issue(retry_requested_at=9.0), sessions)
    assert issue_status(issue(retry_requested_at=9.0), sessions, [], []) is IssueStatus.QUEUED


def test_a_retry_works_even_while_a_session_sleeps():
    sessions = [session("suspended", "inactivity", created_at=1.0)]
    assert wants_session(issue(retry_requested_at=9.0), sessions)


def test_a_stale_retry_does_not_queue_again():
    sessions = [session(created_at=20.0)]
    assert not wants_session(issue(retry_requested_at=9.0), sessions)
