"""Session state interpretation.

The two cases worth reading: a finished task still reports ``running``, and a suspended session is
usually asleep rather than done. Getting either wrong silently corrupts every rate on the dashboard,
which is why they are pinned here rather than left to integration testing.
"""

from __future__ import annotations

import pytest

from app.core.state import (
    IssueState,
    Phase,
    classify,
    is_work_done,
    issue_state_for,
    needs_attention,
)


@pytest.mark.parametrize(
    ("status", "detail", "expected"),
    [
        ("new", None, Phase.STARTING),
        ("claimed", None, Phase.STARTING),
        ("running", "working", Phase.IN_PROGRESS),
        ("running", "waiting_for_user", Phase.BLOCKED),
        ("running", "waiting_for_approval", Phase.BLOCKED),
        ("running", "finished", Phase.COMPLETE),
        ("suspended", "inactivity", Phase.SLEEPING),
        ("suspended", "user_request", Phase.SLEEPING),
        ("suspended", "usage_limit_exceeded", Phase.HALTED_COST),
        ("suspended", "org_usage_limit_exceeded", Phase.HALTED_COST),
        ("suspended", "out_of_credits", Phase.HALTED_COST),
        ("resuming", None, Phase.RESUMING),
        ("exit", None, Phase.ENDED),
        ("error", None, Phase.FAILED),
    ],
)
def test_classify(status, detail, expected):
    assert classify(status, detail) is expected


def test_a_finished_task_still_reports_running():
    """`status == 'exit'` is not the completion signal; `status_detail == 'finished'` is."""
    assert classify("running", "finished") is Phase.COMPLETE


def test_an_unknown_suspension_reads_as_sleep_not_failure():
    """Degrading to a recoverable state keeps a new API value from inflating the failure rate."""
    assert classify("suspended", "some_future_reason") is Phase.SLEEPING


def test_unknown_status_does_not_raise():
    assert classify("teleporting", None) is Phase.IN_PROGRESS


def test_work_is_done_when_the_session_reports_finished():
    assert is_work_done(Phase.COMPLETE, has_structured_output=False, has_pull_request=False)


def test_work_is_done_when_structured_output_exists():
    # The schema is required, so its presence means provide_structured_output was called as final.
    assert is_work_done(Phase.IN_PROGRESS, has_structured_output=True, has_pull_request=False)


def test_a_pr_on_a_sleeping_session_counts_as_done():
    assert is_work_done(Phase.SLEEPING, has_structured_output=False, has_pull_request=True)


def test_a_pr_on_a_running_session_does_not_count_as_done():
    """Devin routinely opens a pull request and keeps working on it."""
    assert not is_work_done(Phase.IN_PROGRESS, has_structured_output=False, has_pull_request=True)


def test_sleeping_without_output_is_not_done():
    assert not is_work_done(Phase.SLEEPING, has_structured_output=False, has_pull_request=False)


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        (Phase.BLOCKED, True),
        (Phase.HALTED_COST, True),
        (Phase.FAILED, True),
        (Phase.SLEEPING, False),
        (Phase.IN_PROGRESS, False),
    ],
)
def test_needs_attention(phase, expected):
    assert needs_attention(phase) is expected


def test_merge_outranks_everything():
    state = issue_state_for(Phase.FAILED, pr_merged=True, pr_open=False, escalated=True)
    assert state is IssueState.MERGED


def test_escalation_outranks_an_open_pr():
    state = issue_state_for(Phase.BLOCKED, pr_merged=False, pr_open=True, escalated=True)
    assert state is IssueState.ESCALATED


def test_a_sleeping_session_with_an_open_pr_is_pr_open():
    state = issue_state_for(Phase.SLEEPING, pr_merged=False, pr_open=True, escalated=False)
    assert state is IssueState.PR_OPEN


def test_a_cost_halt_reads_as_blocked():
    state = issue_state_for(Phase.HALTED_COST, pr_merged=False, pr_open=False, escalated=False)
    assert state is IssueState.BLOCKED
