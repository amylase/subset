"""Persistence behaviour that the metrics depend on being exactly right."""

from __future__ import annotations

from app.db.repo import Repo


def test_delivery_ids_are_idempotent(repo: Repo):
    assert repo.record_delivery("guid-1", "issues", "labeled") is True
    assert repo.record_delivery("guid-1", "issues", "labeled") is False


def test_relabelling_does_not_reset_the_mttr_clock(repo: Repo):
    """`labeled_at` is the MTTR origin. Resetting it on a re-label would flatter every duration."""
    assert repo.upsert_issue(1, "title", "class:security", labeled_at=100.0) is True
    assert repo.upsert_issue(1, "title", "class:security", labeled_at=999.0) is False
    assert repo.issue(1)["labeled_at"] == 100.0


def test_blocked_is_latched_because_the_state_decays_into_sleep(repo: Repo):
    """A session that asked a question sleeps after ~0.1 ACU, so the blocked state is transient."""
    repo.upsert_issue(1, "t", None, labeled_at=0.0)
    repo.create_session("s1", 1, "url", ["tag"])

    repo.update_session(
        "s1", status="running", status_detail="waiting_for_user", acus=0.1, blocked=True
    )
    repo.update_session(
        "s1", status="suspended", status_detail="inactivity", acus=0.2, blocked=False
    )

    assert repo.session("s1")["ever_blocked"] == 1


def test_status_transitions_are_logged_once_per_change(repo: Repo):
    repo.upsert_issue(1, "t", None, labeled_at=0.0)
    repo.create_session("s1", 1, "url", [])

    assert repo.update_session("s1", status="running", status_detail="working", acus=1) is True
    assert repo.update_session("s1", status="running", status_detail="working", acus=2) is False
    assert repo.update_session("s1", status="running", status_detail="finished", acus=3) is True

    events = repo.session_events("s1")
    assert [(e["status"], e["status_detail"]) for e in events] == [
        ("running", "working"),
        ("running", "finished"),
    ]


def test_finished_at_is_written_once(repo: Repo):
    repo.upsert_issue(1, "t", None, labeled_at=0.0)
    repo.create_session("s1", 1, "url", [])
    repo.update_session("s1", status="running", status_detail="finished", acus=1, finished=True)
    first = repo.session("s1")["finished_at"]
    repo.update_session("s1", status="suspended", status_detail="inactivity", acus=2, finished=True)
    assert repo.session("s1")["finished_at"] == first


def test_queue_round_trip(repo: Repo):
    repo.enqueue("issue_labeled", {"number": 7})
    pending = repo.pending_queue()
    assert len(pending) == 1 and pending[0]["payload"] == {"number": 7}
    repo.mark_dispatched(pending[0]["id"])
    assert repo.pending_queue() == []


def test_counters_accumulate(repo: Repo):
    repo.bump("webhook_accepted")
    repo.bump("webhook_accepted")
    repo.bump("webhook_accepted", 3)
    assert repo.counters()["webhook_accepted"] == 5


def test_structured_output_is_not_erased_by_a_later_poll(repo: Repo):
    repo.upsert_issue(1, "t", None, labeled_at=0.0)
    repo.create_session("s1", 1, "url", [])
    repo.update_session(
        "s1",
        status="running",
        status_detail="finished",
        acus=1,
        structured_output={"outcome": "fixed"},
    )
    repo.update_session("s1", status="suspended", status_detail="inactivity", acus=2)
    assert '"outcome": "fixed"' in repo.session("s1")["structured_output"]
