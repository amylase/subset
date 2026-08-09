"""The operating envelope.

These caps are the difference between an autonomous system and an expensive runaway, so each one is
pinned at its boundary.
"""

from __future__ import annotations

from app.core.policy import (
    Envelope,
    can_start_session,
    is_over_session_budget,
    is_startable,
    nudge_or_escalate,
    should_send_ci_feedback,
)
from app.core.state import Phase

ENV = Envelope(
    max_concurrent_sessions=2,
    max_acu_per_session=20,
    global_acu_budget=100.0,
    max_nudges=2,
    max_ci_feedback_rounds=3,
)


def test_a_session_starts_when_there_is_room():
    allowed, reason = can_start_session(ENV, active_sessions=1, acus_spent=10)
    assert allowed and reason == ""


def test_concurrency_cap_blocks_a_start():
    allowed, reason = can_start_session(ENV, active_sessions=2, acus_spent=0)
    assert not allowed and "concurrency" in reason


def test_global_budget_blocks_a_start():
    allowed, reason = can_start_session(ENV, active_sessions=0, acus_spent=100.0)
    assert not allowed and "budget" in reason


def test_nudges_are_bounded_then_escalate():
    """Without a cap, ask -> nudge -> ask burns ACUs until the session ceiling trips."""
    assert nudge_or_escalate(ENV, nudges_sent=0) == "nudge"
    assert nudge_or_escalate(ENV, nudges_sent=1) == "nudge"
    assert nudge_or_escalate(ENV, nudges_sent=2) == "escalate"
    assert nudge_or_escalate(ENV, nudges_sent=9) == "escalate"


def test_ci_feedback_rounds_are_bounded():
    assert should_send_ci_feedback(ENV, rounds_used=2)
    assert not should_send_ci_feedback(ENV, rounds_used=3)


def test_session_budget_boundary():
    assert not is_over_session_budget(ENV, acus=19.9)
    assert is_over_session_budget(ENV, acus=20)


def test_only_a_failed_or_absent_session_permits_a_new_one():
    assert is_startable(None)
    assert is_startable(Phase.FAILED)
    for phase in (Phase.IN_PROGRESS, Phase.SLEEPING, Phase.BLOCKED, Phase.COMPLETE, Phase.ENDED):
        assert not is_startable(phase)
