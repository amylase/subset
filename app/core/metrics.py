"""Metric computation.

Pure functions over rows already loaded from the database, so the arithmetic is testable without
SQLite or a network. Design rules are set out in ``design/metrics.md``; four shape this module.

**Denominate by issue, not by session.** An issue that needed three sessions and got fixed is one
success, not one success and two failures.

**Charge failures to the numerator.** Cost per resolution divides *total* ACUs — including those
burned by sessions that never merged — by the issues actually resolved.

**Split MTTR three ways.** A single duration hides where the time goes. On Superset the split is
roughly agent minutes, CI tens of minutes, human review hours; the bottleneck is not the agent, and
that is the finding worth surfacing.

**Read the same status the dashboard reads.** This module is handed the derived view rather than
recomputing anything, so the two can no longer disagree — in v1 they did, for the same issue.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.state import IssueStatus


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((pct / 100) * (len(ordered) - 1))))
    return ordered[index]


@dataclass
class Durations:
    """MTTR, decomposed. Seconds; ``None`` means not enough data yet."""

    agent: float | None = None  # labeled -> pull request opened
    ci: float | None = None  # pull request opened -> checks settled
    human_review: float | None = None  # checks settled -> merged
    total: float | None = None  # labeled -> merged
    total_p90: float | None = None
    samples: int = 0


@dataclass
class Metrics:
    # tier 1 -- what leadership reads
    issues_total: int = 0
    issues_resolved: int = 0
    resolution_rate: float | None = None
    merge_rate: float | None = None
    autonomy_rate: float | None = None
    durations: Durations = field(default_factory=Durations)
    acus_total: float = 0.0
    acus_wasted: float = 0.0
    cost_per_resolution_usd: float | None = None
    cost_total_usd: float = 0.0
    engineer_hours_saved: float | None = None

    # tier 2 -- operational
    by_status: dict[str, int] = field(default_factory=dict)
    by_class: dict[str, int] = field(default_factory=dict)
    session_outcomes: dict[str, int] = field(default_factory=dict)
    interventions_by_kind: dict[str, int] = field(default_factory=dict)
    interventions_per_resolution: float | None = None
    ci_first_pass_rate: float | None = None
    devin_turns_per_resolution: float | None = None
    attempts_per_issue: float | None = None

    # honesty
    open_notifications: list[dict[str, Any]] = field(default_factory=list)
    notifications_by_reason: dict[str, int] = field(default_factory=dict)
    counters: dict[str, float] = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)


def compute(
    *,
    view: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
    pull_requests: list[dict[str, Any]],
    interventions: list[dict[str, Any]],
    notifications: list[dict[str, Any]],
    counters: dict[str, float],
    acu_unit_cost_usd: float,
    manual_effort_hours_per_issue: float,
    engineer_hourly_usd: float,
) -> Metrics:
    m = Metrics()
    m.counters = counters
    m.issues_total = len(view)

    resolved = [row for row in view if row["status"] == IssueStatus.MERGED]
    m.issues_resolved = len(resolved)
    if m.issues_total:
        m.resolution_rate = m.issues_resolved / m.issues_total

    # Merge rate is pull-request facing; resolution rate is the leadership number.
    opened = [p for p in pull_requests if p["opened_at"]]
    merged = [p for p in opened if p["merged_at"]]
    if opened:
        m.merge_rate = len(merged) / len(opened)

    for row in view:
        key = str(row["status"])
        m.by_status[key] = m.by_status.get(key, 0) + 1
        klass = row["klass"] or "unclassified"
        m.by_class[klass] = m.by_class.get(klass, 0) + 1

    for session in sessions:
        key = (
            session["closed_reason"] or session["status_detail"] or session["status"] or "starting"
        )
        m.session_outcomes[key] = m.session_outcomes.get(key, 0) + 1

    for item in interventions:
        m.interventions_by_kind[item["kind"]] = m.interventions_by_kind.get(item["kind"], 0) + 1

    for note in notifications:
        reason = note["reason_class"]
        m.notifications_by_reason[reason] = m.notifications_by_reason.get(reason, 0) + 1
    m.open_notifications = [n for n in notifications if n["resolved_at"] is None]

    # --- autonomy ----------------------------------------------------------
    # Denominated on the observed outcome — issues that produced a pull request — like every other
    # rate here. Automatic nudges count as intervention: the system compensating for a stall is not
    # the same as not needing to.
    touched = {i["issue_number"] for i in interventions if i["issue_number"] is not None}
    pr_issues = {p["issue_number"] for p in opened if p["issue_number"] is not None}
    delivered = [row for row in view if row["number"] in pr_issues]
    if delivered:
        m.autonomy_rate = sum(1 for r in delivered if r["number"] not in touched) / len(delivered)
    if m.issues_resolved:
        m.interventions_per_resolution = len(interventions) / m.issues_resolved
    if view:
        m.attempts_per_issue = sum(row["attempts"] for row in view) / len(view)

    # --- cost --------------------------------------------------------------
    m.acus_total = sum(float(s["acus"] or 0) for s in sessions)
    merged_session_ids = {p["session_id"] for p in merged if p["session_id"]}
    m.acus_wasted = sum(
        float(s["acus"] or 0) for s in sessions if s["session_id"] not in merged_session_ids
    )
    m.cost_total_usd = m.acus_total * acu_unit_cost_usd
    if m.issues_resolved:
        m.cost_per_resolution_usd = m.cost_total_usd / m.issues_resolved
        m.engineer_hours_saved = m.issues_resolved * manual_effort_hours_per_issue

    # --- durations ---------------------------------------------------------
    labeled_at = {row["number"]: row["first_labeled_at"] for row in view}
    agent, ci, review, total = [], [], [], []
    for pull in pull_requests:
        # Explicit None checks: these are epoch timestamps and a legitimate 0.0 must not read as
        # missing. A truthiness test here silently drops samples.
        start = labeled_at.get(pull["issue_number"])
        opened_at, settled, merged_at = pull["opened_at"], pull["ci_settled_at"], pull["merged_at"]
        if start is not None and opened_at is not None:
            agent.append(opened_at - start)
        if opened_at is not None and settled is not None:
            ci.append(settled - opened_at)
        if settled is not None and merged_at is not None:
            review.append(merged_at - settled)
        if start is not None and merged_at is not None:
            total.append(merged_at - start)

    m.durations = Durations(
        agent=_mean(agent),
        ci=_mean(ci),
        human_review=_mean(review),
        total=_mean(total),
        total_p90=_percentile(total, 90),
        samples=len(total),
    )

    # --- effort and CI -----------------------------------------------------
    turns = [s["devin_messages"] for s in sessions if s.get("devin_messages")]
    if turns and m.issues_resolved:
        m.devin_turns_per_resolution = sum(turns) / m.issues_resolved

    settled_prs = [p for p in pull_requests if p["ci_settled_at"]]
    if settled_prs:
        m.ci_first_pass_rate = sum(1 for p in settled_prs if (p["ci_rounds"] or 0) == 0) / len(
            settled_prs
        )

    # --- assumptions, shown rather than buried -----------------------------
    m.assumptions = [
        f"ACU priced at ${acu_unit_cost_usd:.2f}; actual billing depends on the contract.",
        f"Manual effort assumed at {manual_effort_hours_per_issue:.1f}h per issue at "
        f"${engineer_hourly_usd:.0f}/h — an estimate, not a measurement.",
        f"n = {m.issues_total} issues; rates over a sample this small are indicative only.",
    ]
    return m
