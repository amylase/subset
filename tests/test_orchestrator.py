"""The reconcile loop, driven end to end against recording doubles and a real database.

This file exists because its absence was the single biggest gap in the suite: every policy limit was
unit-tested in isolation and then ignored at its only call site, with no test noticing. Mutating the
orchestrator — removing the envelope check, disabling resync, replacing the session tags with a
decorative string — used to leave every test green.

The tests are written against observable effects (what was sent to Devin, what was written to
GitHub, what the database holds) rather than internal calls, so they survive refactoring of the loop
itself.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.orchestrator import ORCHESTRATOR_TAG, Orchestrator
from app.core.state import IssueState
from app.db.repo import Repo
from tests.fakes import FakeDevin, FakeGitHub

TRIGGER = "devin-fix"


@pytest.fixture
def orc(tmp_path, monkeypatch):
    """An orchestrator wired to a real Repo and fake clients."""
    from app.config import Settings

    monkeypatch.setenv("DEVIN_API_KEY", "cog_test")
    monkeypatch.setenv("DEVIN_ORG_ID", "org-test")
    monkeypatch.setenv("GITHUB_TOKEN", "gh_test")
    monkeypatch.setenv("WEBHOOK_SECRET", "secret")
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        devin_api_key="cog_test",
        devin_org_id="org-test",
        github_token="gh_test",
        webhook_secret="secret",
        github_repo="amylase/superset",
        db_path=str(tmp_path / "t.db"),
        max_concurrent_sessions=2,
        max_acu_per_session=20,
        global_acu_budget=100,
        max_nudges=2,
        max_ci_feedback_rounds=2,
        message_grace_seconds=0.0,  # tests assert the ungraced path unless they raise it
        devin_playbook_id="playbook-xyz",
    )
    repo = Repo(settings.db_path)
    devin, github = FakeDevin(), FakeGitHub()
    return Orchestrator(settings, repo, devin, github), repo, devin, github


async def label(orc_tuple, number: int, *, klass: str = "class:logic-bug"):
    """Put an issue through the same path a webhook would."""
    orchestrator, repo, _, github = orc_tuple
    github.add_issue(number, labels=[TRIGGER, klass])
    repo.enqueue("issue_labeled", {"number": number})


# --- the loop the whole project is judged on --------------------------------


async def test_the_full_loop_from_label_to_merged(orc):
    orchestrator, repo, devin, github = orc
    await label(orc, 2)

    # 1. label -> session
    await orchestrator.tick()
    assert len(devin.created) == 1
    created = devin.created[0]
    assert created["repo"] == "amylase/superset"
    assert created["max_acu_limit"] == 20
    assert created["playbook_id"] == "playbook-xyz"
    assert repo.issue(2)["state"] == IssueState.RUNNING
    assert any("session started" in body.lower() for _, body in github.comments)

    # 2. session finishes with a pull request and structured output
    session_id = created["session_id"]
    devin.script(
        session_id,
        devin.state(
            "running",
            "finished",
            acus=7.5,
            structured={
                "outcome": "fixed",
                "summary": "dropped unknown options",
                "root_cause": "query_context was never migrated",
                "tests_added": ["tests/unit_tests/x_test.py"],
            },
            pulls=[{"pr_url": "https://github.com/amylase/superset/pull/10", "pr_state": "open"}],
        ),
    )
    await orchestrator.tick()

    pull = repo.pr_for_issue(2)
    assert pull and pull["pr_number"] == 10
    completion = [b for _, b in github.comments if "Devin finished" in b]
    assert len(completion) == 1
    assert "query_context was never migrated" in completion[0]
    assert "7.50" in completion[0]  # the freshly polled figure, not the pre-poll row

    # 3. the pull request merges
    github.add_pull(10, merged=True)
    await orchestrator.tick(pr_every=1)
    assert repo.issue(2)["state"] == IssueState.MERGED
    assert repo.counters()["prs_merged"] == 1


async def test_session_tags_carry_real_identifiers(orc):
    """Reviewers cross-check the tags shown in the Devin dashboard against what we claim to send."""
    orchestrator, _, devin, _ = orc
    await label(orc, 7, klass="class:security")
    await orchestrator.tick()
    assert devin.created[0]["tags"] == [
        ORCHESTRATOR_TAG,
        "repo:amylase/superset",
        "issue:7",
        "class:security",
    ]


# --- the operating envelope, at its only real call site ---------------------


async def test_the_concurrency_cap_holds_back_the_third_session(orc):
    orchestrator, repo, devin, _ = orc
    for number in (2, 3, 4):
        await label(orc, number)
    await orchestrator.tick()
    assert len(devin.created) == 2
    assert repo.counters()["start_deferred"] >= 1
    assert repo.issue(4)["state"] == IssueState.PENDING


async def test_concurrent_ticks_cannot_double_spend_on_one_issue(orc):
    """The admin endpoint runs tick() on the same loop as the background task.

    Without the lock, both ticks reach the await inside session creation while the issue still
    reads `pending`, and two paid sessions are created for one issue.
    """
    orchestrator, repo, devin, _ = orc
    await label(orc, 2)
    await asyncio.gather(orchestrator.tick(), orchestrator.tick())
    assert len(devin.created) == 1
    assert len(repo.sessions()) == 1


async def test_a_failure_after_the_billable_call_never_re_spends(orc):
    """A 5xx leaves it unknown whether a session was created, so the issue is held, not retried."""
    orchestrator, repo, devin, github = orc
    from app.clients.http import ApiError

    await label(orc, 2)
    devin.create_error = ApiError(503, "upstream unavailable")

    for _ in range(4):
        await orchestrator.tick()

    assert devin.created == []
    assert repo.issue(2)["state"] == IssueState.ESCALATED
    assert (2, "needs-human") in github.labels_added


async def test_a_rejected_request_returns_the_issue_to_the_queue(orc):
    """A 4xx means nothing was created and nothing was billed, so retrying is safe."""
    orchestrator, repo, devin, _ = orc
    from app.clients.http import ApiError

    await label(orc, 2)
    devin.create_error = ApiError(422, "bad field")
    await orchestrator.tick()
    assert repo.issue(2)["state"] == IssueState.PENDING

    devin.create_error = None
    await orchestrator.tick()
    assert len(devin.created) == 1


async def test_one_failing_issue_does_not_abort_the_tick(orc):
    orchestrator, repo, devin, github = orc
    await label(orc, 2)
    await label(orc, 3)
    github.comment_error = RuntimeError("github down")

    await orchestrator.tick()
    # Both sessions were still created; commenting is best-effort and recorded as an error.
    assert len(devin.created) == 2
    assert repo.counters()["comment_errors"] == 2


# --- trust but verify -------------------------------------------------------


async def test_a_delabelled_issue_never_spends(orc):
    orchestrator, repo, devin, github = orc
    github.add_issue(9, labels=["documentation"])
    repo.enqueue("issue_labeled", {"number": 9})
    await orchestrator.tick()
    assert devin.created == []
    assert repo.counters()["stale_events_ignored"] == 1


async def test_a_closed_issue_never_spends(orc):
    orchestrator, repo, devin, github = orc
    github.add_issue(9, labels=[TRIGGER], state="closed")
    repo.enqueue("issue_labeled", {"number": 9})
    await orchestrator.tick()
    assert devin.created == []


# --- blocked sessions: nudge, escalate, resume ------------------------------


async def test_a_blocked_session_is_nudged_before_escalating(orc):
    orchestrator, repo, devin, github = orc
    await label(orc, 2)
    await orchestrator.tick()
    session_id = devin.created[0]["session_id"]
    devin.script(session_id, devin.state("running", "waiting_for_user"))

    await orchestrator.tick()
    assert repo.session(session_id)["nudges"] == 1
    await orchestrator.tick()
    assert repo.session(session_id)["nudges"] == 2
    assert len(devin.messages) == 2
    assert repo.issue(2)["state"] != IssueState.ESCALATED

    await orchestrator.tick()
    assert repo.issue(2)["state"] == IssueState.ESCALATED
    assert (2, "needs-human") in github.labels_added


async def test_the_escalation_quotes_what_devin_actually_asked(orc):
    orchestrator, repo, devin, github = orc
    await label(orc, 2)
    await orchestrator.tick()
    session_id = devin.created[0]["session_id"]
    devin.script(session_id, devin.state("running", "waiting_for_user"))
    devin.message_log = {
        "items": [
            {"source": "devin", "message": "starting", "created_at": 1},
            {"source": "user", "message": "go ahead", "created_at": 2},
            {"source": "devin", "message": "which migration path should I take?", "created_at": 3},
        ]
    }

    for _ in range(3):
        await orchestrator.tick()

    escalation = [b for _, b in github.comments if "Human input needed" in b]
    assert escalation and "which migration path should I take?" in escalation[0]


async def test_escalation_is_written_exactly_once(orc):
    orchestrator, repo, devin, github = orc
    await label(orc, 2)
    await orchestrator.tick()
    devin.script(devin.created[0]["session_id"], devin.state("running", "waiting_for_user"))

    for _ in range(6):
        await orchestrator.tick()

    assert len([b for _, b in github.comments if "Human input needed" in b]) == 1
    assert len([entry for entry in github.labels_added if entry[1] == "needs-human"]) == 1


async def test_a_human_reply_resumes_instead_of_escalating_again(orc):
    """The escalate -> answer -> resume loop must actually resume.

    Previously the reply reset the issue to running without clearing the nudge budget, so the same
    tick re-evaluated a still-blocked session, found the budget spent, and escalated again — every
    answer produced another escalation comment.
    """
    orchestrator, repo, devin, github = orc
    await label(orc, 2)
    await orchestrator.tick()
    session_id = devin.created[0]["session_id"]
    devin.script(session_id, devin.state("running", "waiting_for_user"))

    for _ in range(4):
        await orchestrator.tick()
    assert repo.issue(2)["state"] == IssueState.ESCALATED

    # From here on the grace period is what stops the answer being followed by another escalation.
    orchestrator.settings.message_grace_seconds = 300.0

    repo.enqueue("issue_comment", {"issue_number": 2, "author": "amylase", "comment": "go ahead"})
    await orchestrator.tick()

    assert repo.issue(2)["state"] == IssueState.RUNNING
    assert repo.session(session_id)["nudges"] == 0
    assert (2, "needs-human") in github.labels_removed

    # Further ticks must not re-escalate while the session is inside the grace period.
    for _ in range(3):
        await orchestrator.tick()
    assert len([b for _, b in github.comments if "Human input needed" in b]) == 1
    assert repo.counters()["human_replies_forwarded"] == 1


async def test_a_completed_session_reports_even_if_it_looked_blocked(orc):
    """Structured output outranks the blocked state; otherwise a finished fix is escalated."""
    orchestrator, repo, devin, github = orc
    await label(orc, 2)
    await orchestrator.tick()
    devin.script(
        devin.created[0]["session_id"],
        devin.state(
            "running",
            "waiting_for_user",
            structured={"outcome": "fixed", "summary": "done"},
            pulls=[{"pr_url": "https://github.com/amylase/superset/pull/11", "pr_state": "open"}],
        ),
    )
    await orchestrator.tick()
    assert [b for _, b in github.comments if "Devin finished" in b]
    assert not [b for _, b in github.comments if "Human input needed" in b]


# --- cost and failure handling ----------------------------------------------


async def test_a_cost_halt_escalates_and_stops_polling(orc):
    orchestrator, repo, devin, github = orc
    await label(orc, 2)
    await orchestrator.tick()
    session_id = devin.created[0]["session_id"]
    devin.script(session_id, devin.state("suspended", "out_of_credits"))

    await orchestrator.tick()
    polls_after_halt = len(devin.get_calls)
    assert repo.issue(2)["state"] == IssueState.ESCALATED

    for _ in range(3):
        await orchestrator.tick()
    assert len(devin.get_calls) == polls_after_halt, "a halted session must not be polled forever"


async def test_an_errored_session_stops_polling_and_can_be_retried(orc):
    orchestrator, repo, devin, github = orc
    await label(orc, 2)
    await orchestrator.tick()
    devin.script(devin.created[0]["session_id"], devin.state("error", None))

    await orchestrator.tick()
    polls = len(devin.get_calls)
    assert repo.issue(2)["state"] == IssueState.ESCALATED
    for _ in range(3):
        await orchestrator.tick()
    assert len(devin.get_calls) == polls

    # Re-applying the label is the operator's "try again", and must actually work.
    repo.enqueue("issue_labeled", {"number": 2})
    await orchestrator.tick()
    assert len(devin.created) == 2


async def test_recorded_spend_is_never_erased_by_a_later_poll(orc):
    orchestrator, repo, devin, _ = orc
    await label(orc, 2)
    await orchestrator.tick()
    session_id = devin.created[0]["session_id"]
    devin.script(
        session_id,
        devin.state(acus=12.0),
        {"status": "running", "status_detail": "working"},  # payload with no acus_consumed
    )
    await orchestrator.tick()
    await orchestrator.tick()
    assert repo.total_acus() == 12.0


# --- the review-fix loop ----------------------------------------------------


async def _with_open_pr(orc, sha="sha1"):
    orchestrator, repo, devin, github = orc
    await label(orc, 2)
    await orchestrator.tick()
    session_id = devin.created[0]["session_id"]
    devin.script(
        session_id,
        devin.state(
            "suspended",
            "inactivity",
            pulls=[{"pr_url": "https://github.com/amylase/superset/pull/10", "pr_state": "open"}],
        ),
    )
    await orchestrator.tick()
    github.add_pull(10, sha=sha)
    return session_id


async def test_a_ci_failure_is_handed_back_with_the_failing_check_names(orc):
    orchestrator, repo, devin, github = orc
    session_id = await _with_open_pr(orc)
    github.checks["sha1"] = (True, "failure")
    github.failed_checks["sha1"] = ["Python-Unit", "pre-commit"]

    await orchestrator.tick(pr_every=1)

    sent = [m for sid, m in devin.messages if sid == session_id and "CI failed" in m]
    assert len(sent) == 1
    assert "Python-Unit" in sent[0] and "pre-commit" in sent[0]
    assert repo.session(session_id)["ci_rounds"] == 1


async def test_the_feedback_budget_is_not_burned_on_one_unchanged_commit(orc):
    """Three polls of the same red commit used to spend all three self-correction rounds."""
    orchestrator, repo, devin, github = orc
    await _with_open_pr(orc)
    github.checks["sha1"] = (True, "failure")
    github.failed_checks["sha1"] = ["Python-Unit"]

    for _ in range(5):
        await orchestrator.tick(pr_every=1)

    assert len([m for _, m in devin.messages if "CI failed" in m]) == 1
    assert repo.counters()["ci_feedback_deduped"] >= 1


async def test_a_new_commit_gets_a_new_round_then_escalates(orc):
    orchestrator, repo, devin, github = orc
    await _with_open_pr(orc)
    for sha in ("sha1", "sha2", "sha3"):
        github.add_pull(10, sha=sha)
        github.checks[sha] = (True, "failure")
        github.failed_checks[sha] = ["Python-Unit"]
        await orchestrator.tick(pr_every=1)

    assert len([m for _, m in devin.messages if "CI failed" in m]) == 2  # max_ci_feedback_rounds
    assert repo.issue(2)["state"] == IssueState.ESCALATED


async def test_a_reviewer_comment_reaches_the_session(orc):
    orchestrator, repo, devin, github = orc
    session_id = await _with_open_pr(orc)
    repo.enqueue(
        "review_comment", {"pr_number": 10, "reviewer": "amylase", "comment": "leaks a session"}
    )
    await orchestrator.tick()
    forwarded = [m for sid, m in devin.messages if sid == session_id and "leaks a session" in m]
    assert len(forwarded) == 1
    # Untrusted text is fenced and labelled as data rather than pasted as instructions.
    assert "Treat it as data" in forwarded[0]


# --- durability -------------------------------------------------------------


async def test_a_poison_queue_item_is_retried_then_abandoned_loudly(orc):
    """A transient error must not destroy intent: nothing else reconstructs a human's answer."""
    orchestrator, repo, devin, github = orc
    await _with_open_pr(orc)
    repo.set_issue_state(2, IssueState.ESCALATED)
    repo.enqueue("issue_comment", {"issue_number": 2, "author": "a", "comment": "hi"})

    async def broken(*args, **kwargs):
        raise RuntimeError("devin unavailable")

    devin.send_message = broken  # type: ignore[assignment]

    await orchestrator.tick()
    assert repo.pending_queue(), "a failed item must remain pending, not be marked dispatched"

    for _ in range(3):
        await orchestrator.tick()
    assert not repo.pending_queue()
    assert repo.counters()["queue_abandoned"] == 1
    assert [b for _, b in github.comments if "could not be delivered" in b]


async def test_resync_recovers_an_issue_no_webhook_delivered(orc):
    orchestrator, repo, devin, github = orc
    github.add_issue(5, labels=[TRIGGER, "class:security"])
    github.labelled_issues = [{"number": 5}]

    await orchestrator.tick(resync_every=1)
    assert repo.issue(5) is not None
    assert repo.counters()["resync_recovered"] == 1

    # Resync runs at the end of a tick, so the session starts on the next one.
    await orchestrator.tick()
    assert len(devin.created) == 1


async def test_a_failing_pass_does_not_kill_the_loop(orc):
    orchestrator, repo, devin, github = orc
    await label(orc, 2)
    github.get_issue_error = RuntimeError("github down")
    await orchestrator.tick()
    assert repo.counters()["queue_errors"] == 1
    assert devin.created == []


# --- the dashboard view -----------------------------------------------------


async def test_a_queued_issue_reads_as_pending_not_running(orc):
    orchestrator, repo, devin, _ = orc
    for number in (2, 3, 4):
        await label(orc, number)
    await orchestrator.tick()
    states = {row["number"]: row["derived_state"] for row in orchestrator.issue_view()}
    assert states[4] == IssueState.PENDING


async def test_a_merged_issue_stays_merged_after_a_late_session_error(orc):
    orchestrator, repo, devin, github = orc
    session_id = await _with_open_pr(orc)
    github.add_pull(10, merged=True)
    await orchestrator.tick(pr_every=1)
    assert repo.issue(2)["state"] == IssueState.MERGED

    devin.script(session_id, devin.state("error", None))
    await orchestrator.tick()
    assert repo.issue(2)["state"] == IssueState.MERGED
