"""Metric arithmetic.

The tests that matter are the ones that would otherwise let a flattering number ship: counting per
session instead of per issue, and excluding failed work from cost.
"""

from __future__ import annotations

import pytest

from app.core.metrics import compute
from app.core.state import IssueStatus

HOUR = 3600.0


def make(view=None, sessions=None, pulls=None, interventions=None, notifications=None):
    return compute(
        view=view or [],
        sessions=sessions or [],
        pull_requests=pulls or [],
        interventions=interventions or [],
        notifications=notifications or [],
        counters={},
        acu_unit_cost_usd=2.0,
        manual_effort_hours_per_issue=4.0,
        engineer_hourly_usd=100.0,
    )


def row(number: int, status=IssueStatus.MERGED, *, labeled_at=0.0, klass=None, attempts=1):
    return {
        "number": number,
        "title": f"issue {number}",
        "klass": klass,
        "first_labeled_at": labeled_at,
        "status": status,
        "attempts": attempts,
    }


def session(sid: str, acus: float, *, detail="finished", closed=None, turns=None):
    return {
        "session_id": sid,
        "acus": acus,
        "status": "running",
        "status_detail": detail,
        "closed_reason": closed,
        "devin_messages": turns,
    }


def pull(number: int, issue_number: int, sid: str, *, opened, ci=None, merged=None, rounds=0):
    return {
        "pr_number": number,
        "issue_number": issue_number,
        "session_id": sid,
        "opened_at": opened,
        "ci_settled_at": ci,
        "ci_conclusion": "success" if ci else None,
        "ci_rounds": rounds,
        "merged_at": merged,
        "closed_at": None,
    }


def test_resolution_rate_is_denominated_by_issue_not_session():
    """One issue that needed three sessions and got fixed is one success, not one in three."""
    m = make(
        view=[row(1, attempts=3)],
        sessions=[session("s1", 5), session("s2", 5), session("s3", 5)],
        pulls=[pull(10, 1, "s3", opened=HOUR, merged=2 * HOUR)],
    )
    assert (m.issues_total, m.issues_resolved, m.resolution_rate) == (1, 1, 1.0)
    assert m.attempts_per_issue == 3


def test_cost_per_resolution_includes_acus_from_failed_sessions():
    """Excluding failed attempts would report $10 here instead of the true $30."""
    m = make(
        view=[row(1)],
        sessions=[session("s1", 5), session("s2", 5), session("s3", 5)],
        pulls=[pull(10, 1, "s3", opened=HOUR, merged=2 * HOUR)],
    )
    assert m.acus_total == 15
    assert m.acus_wasted == 10
    assert m.cost_per_resolution_usd == 30.0


def test_mttr_is_split_three_ways():
    m = make(
        view=[row(1, labeled_at=0.0)],
        sessions=[session("s1", 4)],
        pulls=[pull(10, 1, "s1", opened=HOUR, ci=2 * HOUR, merged=10 * HOUR)],
    )
    d = m.durations
    assert (d.agent, d.ci, d.human_review, d.total, d.samples) == (
        HOUR,
        HOUR,
        8 * HOUR,
        10 * HOUR,
        1,
    )


def test_human_review_dominating_is_visible_not_hidden():
    m = make(
        view=[row(1)],
        sessions=[session("s1", 4)],
        pulls=[pull(10, 1, "s1", opened=0.2 * HOUR, ci=HOUR, merged=24 * HOUR)],
    )
    assert m.durations.human_review > m.durations.agent * 10


def test_merge_rate_is_over_opened_pull_requests():
    """Asymmetric on purpose: with equal counts both candidate denominators agree, so the test
    could not distinguish the property it claims to pin."""
    m = make(
        view=[row(1), row(2, IssueStatus.PR_OPEN), row(3, IssueStatus.QUEUED)],
        sessions=[session("s1", 4), session("s2", 4)],
        pulls=[pull(10, 1, "s1", opened=HOUR, merged=2 * HOUR), pull(11, 2, "s2", opened=HOUR)],
    )
    assert m.merge_rate == 0.5
    assert m.resolution_rate == pytest.approx(1 / 3)


def test_autonomy_rate_counts_nudges_as_intervention():
    """An automatic nudge is the system compensating for a stall, not the absence of one."""
    m = make(
        view=[row(1), row(2)],
        sessions=[session("s1", 4), session("s2", 4)],
        pulls=[
            pull(10, 1, "s1", opened=HOUR, merged=2 * HOUR),
            pull(11, 2, "s2", opened=HOUR, merged=2 * HOUR),
        ],
        interventions=[{"issue_number": 2, "kind": "auto_nudge", "at": 1.0}],
    )
    assert m.autonomy_rate == 0.5


def test_ci_first_pass_rate_excludes_pull_requests_that_needed_the_fix_loop():
    m = make(
        view=[row(1), row(2)],
        sessions=[session("s1", 4), session("s2", 4)],
        pulls=[
            pull(10, 1, "s1", opened=HOUR, ci=2 * HOUR, merged=3 * HOUR, rounds=0),
            pull(11, 2, "s2", opened=HOUR, ci=2 * HOUR, merged=3 * HOUR, rounds=2),
        ],
    )
    assert m.ci_first_pass_rate == 0.5


def test_open_notifications_are_surfaced():
    """The honesty surface: a system that has stopped working must say so on the dashboard."""
    m = make(
        view=[row(1, IssueStatus.AWAITING_HUMAN)],
        notifications=[
            {"issue_number": 1, "reason_class": "cost_halt", "resolved_at": None},
            {"issue_number": 1, "reason_class": "blocked_on_question", "resolved_at": 5.0},
        ],
    )
    assert [n["reason_class"] for n in m.open_notifications] == ["cost_halt"]
    assert m.notifications_by_reason == {"cost_halt": 1, "blocked_on_question": 1}


def test_status_counts_come_from_the_derived_view():
    m = make(view=[row(1), row(2, IssueStatus.QUEUED), row(3, IssueStatus.QUEUED)])
    assert m.by_status == {"merged": 1, "queued": 2}


def test_devin_turns_per_resolution():
    m = make(
        view=[row(1)],
        sessions=[session("s1", 4, turns=12)],
        pulls=[pull(10, 1, "s1", opened=HOUR, merged=2 * HOUR)],
    )
    assert m.devin_turns_per_resolution == 12


def test_empty_state_reports_none_rather_than_zero():
    """A rate of 0% over no data is a lie; an absent value is honest."""
    m = make()
    assert m.resolution_rate is None
    assert m.merge_rate is None
    assert m.cost_per_resolution_usd is None
    assert m.durations.total is None
    assert m.autonomy_rate is None


def test_assumptions_are_always_reported():
    m = make(view=[row(1, IssueStatus.QUEUED)])
    assert any("ACU priced" in a for a in m.assumptions)
    assert any("n = 1" in a for a in m.assumptions)
