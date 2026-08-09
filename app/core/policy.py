"""The operating envelope: what the orchestrator is and is not allowed to do.

Every decision that can spend money or annoy a human is made here, as a pure function of recorded
state. The reconcile loop is the only caller, which is what makes "one place enforces the limits"
true rather than aspirational.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.state import Phase


@dataclass(frozen=True)
class Envelope:
    max_concurrent_sessions: int
    max_acu_per_session: int
    global_acu_budget: float
    max_nudges: int
    max_ci_feedback_rounds: int


def can_start_session(
    envelope: Envelope,
    *,
    active_sessions: int,
    acus_spent: float,
) -> tuple[bool, str]:
    """Whether a new session may be created. Returns ``(allowed, reason_if_not)``."""
    if active_sessions >= envelope.max_concurrent_sessions:
        return (
            False,
            f"concurrency cap reached ({active_sessions}/{envelope.max_concurrent_sessions})",
        )
    if acus_spent >= envelope.global_acu_budget:
        return False, f"global ACU budget exhausted ({acus_spent:.1f}/{envelope.global_acu_budget})"
    return True, ""


def nudge_or_escalate(envelope: Envelope, *, nudges_sent: int) -> str:
    """What to do about a session that stopped to ask a question.

    Bounded on purpose. Without a cap the ask -> nudge -> ask cycle burns ACUs until the per-session
    ceiling trips, and that ceiling is meant to be a backstop, not the control loop.
    """
    return "nudge" if nudges_sent < envelope.max_nudges else "escalate"


def should_send_ci_feedback(envelope: Envelope, *, rounds_used: int) -> bool:
    """Whether to hand another CI failure back to Devin.

    Capped for the same reason as nudges: a session that cannot get CI green in a few attempts is
    not going to get there on the next one, and a human should look at it.
    """
    return rounds_used < envelope.max_ci_feedback_rounds


def is_over_session_budget(envelope: Envelope, *, acus: float) -> bool:
    """Local mirror of ``max_acu_limit``.

    The API enforces the ceiling itself; this exists so the dashboard can show a cap trip as an
    orchestrator decision rather than only as an opaque ``usage_limit_exceeded`` suspension.
    """
    return acus >= envelope.max_acu_per_session


def is_startable(phase: Phase | None) -> bool:
    """Whether an issue has no session worth waiting on, so a new one may be created."""
    return phase is None or phase is Phase.FAILED
