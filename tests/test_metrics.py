"""Metric arithmetic.

The tests that matter here are the ones that would otherwise let a flattering number ship: counting
per session instead of per issue, and excluding failed work from cost.
"""

from __future__ import annotations

from app.core.metrics import compute

HOUR = 3600.0


def make(
    issues: list[dict] | None = None,
    sessions: list[dict] | None = None,
    pulls: list[dict] | None = None,
    interventions: list[dict] | None = None,
):
    return compute(
        issues=issues or [],
        sessions=sessions or [],
        pull_requests=pulls or [],
        interventions=interventions or [],
        counters={},
        acu_unit_cost_usd=2.0,
        manual_effort_hours_per_issue=4.0,
        engineer_hourly_usd=100.0,
    )


def issue(number: int, state: str = "merged", labeled_at: float = 0.0, klass: str | None = None):
    return {
        "number": number,
        "title": f"issue {number}",
        "klass": klass,
        "labeled_at": labeled_at,
        "state": state,
        "updated_at": 0.0,
    }


def session(sid: str, issue_number: int, acus: float, status="running", detail="finished"):
    return {
        "session_id": sid,
        "issue_number": issue_number,
        "url": "https://app.devin.ai/sessions/x",
        "acus": acus,
        "status": status,
        "status_detail": detail,
        "nudges": 0,
        "ci_rounds": 0,
        "finished_at": 1.0,
        "structured_output": None,
    }


def pull(number: int, issue_number: int, sid: str, *, opened, ci=None, merged=None, attempts=0):
    return {
        "pr_number": number,
        "issue_number": issue_number,
        "session_id": sid,
        "url": f"https://github.com/x/y/pull/{number}",
        "opened_at": opened,
        "ci_settled_at": ci,
        "ci_conclusion": "success" if ci else None,
        "ci_attempts": attempts,
        "merged_at": merged,
        "closed_at": None,
        "state": "merged" if merged else "open",
    }


def test_resolution_rate_is_denominated_by_issue_not_session():
    """One issue that needed three sessions and got fixed is one success, not one in three."""
    m = make(
        issues=[issue(1)],
        sessions=[session("s1", 1, 5), session("s2", 1, 5), session("s3", 1, 5)],
        pulls=[pull(10, 1, "s3", opened=HOUR, merged=2 * HOUR)],
    )
    assert m.issues_total == 1
    assert m.issues_resolved == 1
    assert m.resolution_rate == 1.0


def test_cost_per_resolution_includes_acus_from_failed_sessions():
    """Excluding failed attempts would report $10 here instead of the true $30."""
    m = make(
        issues=[issue(1)],
        sessions=[session("s1", 1, 5), session("s2", 1, 5), session("s3", 1, 5)],
        pulls=[pull(10, 1, "s3", opened=HOUR, merged=2 * HOUR)],
    )
    assert m.acus_total == 15
    assert m.acus_wasted == 10  # s1 and s2 produced nothing that merged
    assert m.cost_per_resolution_usd == 30.0  # 15 ACU * $2, over one resolution


def test_mttr_is_split_three_ways():
    m = make(
        issues=[issue(1, labeled_at=0.0)],
        sessions=[session("s1", 1, 4)],
        pulls=[pull(10, 1, "s1", opened=HOUR, ci=2 * HOUR, merged=10 * HOUR)],
    )
    d = m.durations
    assert d.agent == HOUR  # labeled -> PR opened
    assert d.ci == HOUR  # PR opened -> checks settled
    assert d.human_review == 8 * HOUR  # checks settled -> merged
    assert d.total == 10 * HOUR
    assert d.samples == 1


def test_human_review_dominates_is_visible_not_hidden():
    """The whole point of the split: the bottleneck is legible."""
    m = make(
        issues=[issue(1)],
        sessions=[session("s1", 1, 4)],
        pulls=[pull(10, 1, "s1", opened=0.2 * HOUR, ci=HOUR, merged=24 * HOUR)],
    )
    assert m.durations.human_review > m.durations.agent * 10


def test_autonomy_rate_counts_nudges_as_intervention():
    """An automatic nudge is still the system compensating for a stall, so it is not autonomous."""
    m = make(
        issues=[issue(1, state="merged"), issue(2, state="merged")],
        sessions=[session("s1", 1, 4), session("s2", 2, 4)],
        pulls=[
            pull(10, 1, "s1", opened=HOUR, merged=2 * HOUR),
            pull(11, 2, "s2", opened=HOUR, merged=2 * HOUR),
        ],
        interventions=[{"issue_number": 2, "kind": "auto_nudge", "at": 1.0}],
    )
    assert m.autonomy_rate == 0.5


def test_merge_rate_is_over_opened_pull_requests():
    m = make(
        issues=[issue(1), issue(2, state="pr_open")],
        sessions=[session("s1", 1, 4), session("s2", 2, 4)],
        pulls=[
            pull(10, 1, "s1", opened=HOUR, merged=2 * HOUR),
            pull(11, 2, "s2", opened=HOUR),
        ],
    )
    assert m.merge_rate == 0.5
    assert m.resolution_rate == 0.5


def test_ci_first_pass_rate_excludes_pull_requests_that_needed_the_fix_loop():
    m = make(
        issues=[issue(1), issue(2)],
        sessions=[session("s1", 1, 4), session("s2", 2, 4)],
        pulls=[
            pull(10, 1, "s1", opened=HOUR, ci=2 * HOUR, merged=3 * HOUR, attempts=0),
            pull(11, 2, "s2", opened=HOUR, ci=2 * HOUR, merged=3 * HOUR, attempts=2),
        ],
    )
    assert m.ci_first_pass_rate == 0.5


def test_empty_state_reports_none_rather_than_zero():
    """A rate of 0% over no data is a lie; an absent value is honest."""
    m = make()
    assert m.resolution_rate is None
    assert m.merge_rate is None
    assert m.cost_per_resolution_usd is None
    assert m.durations.total is None


def test_assumptions_are_always_reported():
    m = make(issues=[issue(1, state="pending")])
    assert any("ACU priced" in a for a in m.assumptions)
    assert any("n = 1" in a for a in m.assumptions)
