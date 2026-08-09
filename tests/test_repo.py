"""Persistence behaviour the rest of the system depends on being exactly right."""

from __future__ import annotations

from app.db.repo import Repo


def seed(repo: Repo, number: int = 2) -> None:
    repo.register_issue(number, "t", "class:logic-bug")
    repo.create_session("s1", number, url="u", tags=["tag"], attempt=1)


# --- the effects ledger ------------------------------------------------------


def test_a_key_can_be_claimed_once(repo: Repo):
    assert repo.claim_effect("k", "comment") is True
    assert repo.claim_effect("k", "comment") is False


def test_a_released_key_can_be_claimed_again(repo: Repo):
    """v1 had no release, so a CI feedback attempt that could not name the failing checks burned
    the key and the retry never fired once the names appeared."""
    repo.claim_effect("k", "message")
    repo.release_effect("k")
    assert repo.claim_effect("k", "message") is True


def test_a_confirmed_key_cannot_be_released(repo: Repo):
    repo.claim_effect("k", "message")
    repo.confirm_effect("k")
    repo.release_effect("k")
    assert repo.claim_effect("k", "message") is False


# --- notifications -----------------------------------------------------------


def test_one_open_notification_per_reason_class(repo: Repo):
    seed(repo)
    assert repo.open_notification(2, "blocked", session_id=None, detail="d") is True
    assert repo.open_notification(2, "blocked", session_id=None, detail="d") is False
    assert repo.open_notification(2, "cost_halt", session_id=None, detail="d") is True
    assert {n["reason_class"] for n in repo.open_notifications(2)} == {"blocked", "cost_halt"}


def test_a_resolved_reason_can_recur(repo: Repo):
    seed(repo)
    repo.open_notification(2, "blocked", session_id=None, detail="d")
    repo.resolve_notifications(2, "blocked")
    assert repo.open_notifications(2) == []
    assert repo.open_notification(2, "blocked", session_id=None, detail="again") is True


def test_resolving_without_a_class_closes_them_all(repo: Repo):
    seed(repo)
    repo.open_notification(2, "a", session_id=None, detail="")
    repo.open_notification(2, "b", session_id=None, detail="")
    assert repo.resolve_notifications(2) == 2
    assert repo.open_notifications(2) == []


# --- issues ------------------------------------------------------------------


def test_re_registering_does_not_reset_the_mttr_clock(repo: Repo):
    """`first_labeled_at` is the MTTR origin; resetting it would flatter every duration."""
    assert repo.register_issue(1, "t", None) is True
    first = repo.issue(1)["first_labeled_at"]
    assert repo.register_issue(1, "t", None) is False
    assert repo.issue(1)["first_labeled_at"] == first


def test_a_retry_is_a_timestamp_not_a_state(repo: Repo):
    repo.register_issue(1, "t", None)
    assert repo.issue(1)["retry_requested_at"] is None
    repo.request_retry(1)
    assert repo.issue(1)["retry_requested_at"] is not None


# --- sessions ----------------------------------------------------------------


def test_blocked_is_latched_because_the_state_decays_into_sleep(repo: Repo):
    seed(repo)
    repo.record_poll(
        "s1", status="running", status_detail="waiting_for_user", acus=0.1, blocked=True
    )
    repo.record_poll("s1", status="suspended", status_detail="inactivity", acus=0.2, blocked=False)
    assert repo.session("s1")["ever_blocked"] == 1


def test_spend_only_ever_increases(repo: Repo):
    seed(repo)
    repo.record_poll("s1", status="running", status_detail="working", acus=9.0)
    repo.record_poll("s1", status="running", status_detail="working", acus=0.0)
    assert repo.total_acus() == 9.0


def test_produced_and_closed_are_independent(repo: Repo):
    seed(repo)
    repo.record_poll("s1", status="suspended", status_detail="inactivity", acus=1, produced=True)
    row = repo.session("s1")
    assert row["produced_at"] is not None
    assert row["closed_at"] is None, "a session that produced work is still wakeable"

    repo.record_poll("s1", status="exit", status_detail=None, acus=1, closed_reason="exit")
    assert repo.session("s1")["closed_at"] is not None


def test_both_timestamps_are_written_once(repo: Repo):
    seed(repo)
    repo.record_poll("s1", status="running", status_detail="finished", acus=1, produced=True)
    first = repo.session("s1")["produced_at"]
    repo.record_poll("s1", status="exit", status_detail=None, acus=1, produced=True)
    assert repo.session("s1")["produced_at"] == first


def test_status_transitions_are_logged_once_per_change(repo: Repo):
    seed(repo)
    assert repo.record_poll("s1", status="running", status_detail="working", acus=1) is True
    assert repo.record_poll("s1", status="running", status_detail="working", acus=2) is False
    assert repo.record_poll("s1", status="running", status_detail="finished", acus=3) is True
    assert [(e["status"], e["status_detail"]) for e in repo.session_events("s1")] == [
        ("running", "working"),
        ("running", "finished"),
    ]


def test_structured_output_is_not_erased_by_a_later_poll(repo: Repo):
    seed(repo)
    repo.record_poll(
        "s1",
        status="running",
        status_detail="finished",
        acus=1,
        structured_output={"outcome": "fixed"},
    )
    repo.record_poll("s1", status="suspended", status_detail="inactivity", acus=2)
    assert '"outcome": "fixed"' in repo.session("s1")["structured_output"]


def test_resetting_budgets_clears_nudges_and_ci_rounds(repo: Repo):
    seed(repo)
    repo.bump_nudges("s1")
    repo.bump_nudges("s1")
    repo.upsert_pr(10, issue_number=2, session_id="s1", url="u", opened_at=1.0)
    repo.update_pr(10, ci_rounds=3)

    repo.reset_budgets("s1")
    assert repo.session("s1")["nudges"] == 0
    assert repo.pull_request(10)["ci_rounds"] == 0


def test_a_sparse_insight_row_preserves_known_values(repo: Repo):
    seed(repo)
    repo.apply_insight("s1", acus=5.0, devin_messages=7, user_messages=2, session_size="m")
    repo.apply_insight("s1", acus=6.0, devin_messages=None, user_messages=None, session_size=None)
    row = repo.session("s1")
    assert (row["acus"], row["devin_messages"], row["session_size"]) == (6.0, 7, "m")


def test_an_insight_for_an_unknown_session_is_refused(repo: Repo):
    assert (
        repo.apply_insight(
            "not-ours", acus=500.0, devin_messages=1, user_messages=1, session_size="xl"
        )
        is False
    )


# --- inbox and deliveries ----------------------------------------------------


def test_delivery_ids_are_idempotent(repo: Repo):
    assert repo.record_delivery("guid", "issues", "labeled") is True
    assert repo.record_delivery("guid", "issues", "labeled") is False


def test_inbox_round_trip(repo: Repo):
    repo.enqueue("issue_labeled", {"number": 7}, provenance="system")
    pending = repo.pending_inbox()
    assert len(pending) == 1
    assert pending[0]["payload"] == {"number": 7}
    assert pending[0]["provenance"] == "system"
    repo.mark_dispatched(pending[0]["id"])
    assert repo.pending_inbox() == []


def test_inbox_failures_are_counted_then_exhausted(repo: Repo):
    inbox_id = repo.enqueue("issue_comment", {}, provenance="trusted")
    assert repo.record_inbox_failure(inbox_id, "boom", max_attempts=3) is False
    assert repo.record_inbox_failure(inbox_id, "boom", max_attempts=3) is False
    assert repo.record_inbox_failure(inbox_id, "boom", max_attempts=3) is True
    assert repo.pending_inbox() == []


def test_a_failure_on_a_missing_row_is_not_reported_as_exhausted(repo: Repo):
    assert repo.record_inbox_failure(9999, "boom", max_attempts=1) is False


# --- pull requests -----------------------------------------------------------


def test_tracked_pull_requests_exclude_settled_ones(repo: Repo):
    seed(repo)
    repo.upsert_pr(10, issue_number=2, session_id="s1", url="u", opened_at=1.0)
    repo.upsert_pr(11, issue_number=2, session_id="s1", url="u", opened_at=1.0)
    repo.update_pr(11, merged_at=2.0)
    assert [p["pr_number"] for p in repo.tracked_pull_requests()] == [10]


def test_counters_accumulate(repo: Repo):
    repo.bump("webhook_accepted")
    repo.bump("webhook_accepted", 4)
    assert repo.counters()["webhook_accepted"] == 5
