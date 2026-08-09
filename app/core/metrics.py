"""Metric computation.

Pure functions over rows already loaded from the database, so the arithmetic is testable without
touching SQLite or the network. The design rules, and why they matter, are set out in
``design/metrics.md``; the three that shape this module:

**Denominate by issue, not by session.** An issue that needed three sessions and got fixed is one
success, not one success and two failures. Session-level numbers exist, but they live in the
reliability tier where they describe machinery rather than outcomes.

**Charge failures to the numerator.** Cost per resolution divides *total* ACUs — including those
burned by sessions that never merged — by the number of issues actually resolved. Dividing only
successful sessions' ACUs by successful outcomes produces a flattering number that means nothing.

**Split MTTR three ways.** A single duration hides where time goes. Measured on Superset the split
is roughly agent minutes, CI tens of minutes, human review hours — which says the bottleneck is not
the agent, and that is the finding worth surfacing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
    """MTTR, decomposed. Values are seconds; ``None`` means not enough data yet."""

    agent: float | None = None  # issue labeled -> pull request opened
    ci: float | None = None  # pull request opened -> checks settled
    human_review: float | None = None  # checks settled -> merged
    total: float | None = None  # issue labeled -> merged
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
    by_state: dict[str, int] = field(default_factory=dict)
    by_class: dict[str, int] = field(default_factory=dict)
    session_outcomes: dict[str, int] = field(default_factory=dict)
    interventions_by_kind: dict[str, int] = field(default_factory=dict)
    interventions_per_resolution: float | None = None
    ci_first_pass_rate: float | None = None
    counters: dict[str, float] = field(default_factory=dict)

    # honesty
    assumptions: list[str] = field(default_factory=list)


def compute(
    *,
    issues: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
    pull_requests: list[dict[str, Any]],
    interventions: list[dict[str, Any]],
    counters: dict[str, float],
    acu_unit_cost_usd: float,
    manual_effort_hours_per_issue: float,
    engineer_hourly_usd: float,
) -> Metrics:
    m = Metrics()
    m.counters = counters

    m.issues_total = len(issues)
    # One pull request per issue, preferring a merged one. Devin can open more than one pull
    # request for an issue; keeping simply the last row reported the issue unresolved even when an
    # earlier pull request had merged, which silently zeroed the headline number.
    pr_by_issue: dict[int, dict[str, Any]] = {}
    for p in pull_requests:
        key = p["issue_number"]
        if key is None:
            continue
        current = pr_by_issue.get(key)
        if current is None or (p["merged_at"] and not current["merged_at"]):
            pr_by_issue[key] = p

    resolved = [i for i in issues if (pr_by_issue.get(i["number"]) or {}).get("merged_at")]
    m.issues_resolved = len(resolved)
    if m.issues_total:
        m.resolution_rate = m.issues_resolved / m.issues_total

    # Merge rate is session-facing (PRs opened vs merged); resolution rate is the leadership number.
    opened = [p for p in pull_requests if p["opened_at"]]
    merged = [p for p in opened if p["merged_at"]]
    if opened:
        m.merge_rate = len(merged) / len(opened)

    # --- state and class breakdowns ---------------------------------------
    for issue in issues:
        state = issue["state"]
        m.by_state[state] = m.by_state.get(state, 0) + 1
        klass = issue["klass"] or "unclassified"
        m.by_class[klass] = m.by_class.get(klass, 0) + 1

    for session in sessions:
        key = session["status_detail"] or session["status"] or "unknown"
        m.session_outcomes[key] = m.session_outcomes.get(key, 0) + 1

    for item in interventions:
        m.interventions_by_kind[item["kind"]] = m.interventions_by_kind.get(item["kind"], 0) + 1

    # --- autonomy ----------------------------------------------------------
    # An issue counts as autonomous when no intervention of any kind was recorded against it.
    # Nudges count: an automatic nudge is still the system compensating for a stall.
    touched = {i["issue_number"] for i in interventions if i["issue_number"] is not None}
    # Denominated on the observed outcome — issues that produced a pull request — like every other
    # rate here. Reading the stored issue state instead made this the one metric with a different
    # vocabulary, and it returned None whenever that column disagreed with what GitHub showed.
    delivered = [i for i in issues if i["number"] in pr_by_issue]
    if delivered:
        m.autonomy_rate = sum(1 for i in delivered if i["number"] not in touched) / len(delivered)
    if m.issues_resolved:
        m.interventions_per_resolution = len(interventions) / m.issues_resolved

    # --- cost --------------------------------------------------------------
    m.acus_total = sum(float(s["acus"] or 0) for s in sessions)
    merged_session_ids = {p["session_id"] for p in merged if p["session_id"]}
    m.acus_wasted = sum(
        float(s["acus"] or 0) for s in sessions if s["session_id"] not in merged_session_ids
    )
    m.cost_total_usd = m.acus_total * acu_unit_cost_usd
    if m.issues_resolved:
        # Total ACUs, including failures, over issues actually resolved.
        m.cost_per_resolution_usd = m.cost_total_usd / m.issues_resolved
        m.engineer_hours_saved = m.issues_resolved * manual_effort_hours_per_issue

    # --- durations ---------------------------------------------------------
    labeled_at = {i["number"]: i["labeled_at"] for i in issues}
    agent, ci, review, total = [], [], [], []
    for pull in pull_requests:
        # Explicit None checks throughout: these are epoch timestamps, and a legitimate 0.0 must not
        # be read as "missing". A truthiness test here silently drops samples.
        start = labeled_at.get(pull["issue_number"])
        opened, settled, merged_at = pull["opened_at"], pull["ci_settled_at"], pull["merged_at"]
        if start is not None and opened is not None:
            agent.append(opened - start)
        if opened is not None and settled is not None:
            ci.append(settled - opened)
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

    # --- CI first-pass rate ------------------------------------------------
    # Pull requests whose checks went green without the review-fix loop being invoked.
    settled = [p for p in pull_requests if p["ci_settled_at"]]
    if settled:
        m.ci_first_pass_rate = sum(1 for p in settled if (p["ci_attempts"] or 0) == 0) / len(
            settled
        )

    # --- assumptions, shown on screen rather than buried -------------------
    m.assumptions = [
        f"ACU priced at ${acu_unit_cost_usd:.2f}; actual billing depends on the contract.",
        f"Manual effort assumed at {manual_effort_hours_per_issue:.1f}h per issue "
        f"at ${engineer_hourly_usd:.0f}/h — an estimate, not a measurement.",
        f"n = {m.issues_total} issues; rates over a sample this small are indicative only.",
    ]
    return m
