"""The reconcile loop, driven end to end against recording doubles and a real database.

Written against observable effects — what was sent to Devin, what was written to GitHub, what the
database holds — rather than internal calls, so they survive refactoring of the loop.

Several tests exist specifically because their absence let a defect ship in v1; those say so.
"""

from __future__ import annotations

import asyncio

import pytest

from app.clients.http import ApiError
from app.core import orchestrator as orchestrator_module
from app.core.orchestrator import ORCHESTRATOR_TAG, Orchestrator, parse_pr_number
from app.core.state import IssueStatus
from app.db import repo as repo_module
from app.webhooks.handlers import EMITTED_KINDS

TRIGGER = "devin-fix"
PR_URL = "https://github.com/amylase/superset/pull/10"


def label(orc_tuple, number: int, *, klass: str = "class:logic-bug"):
    _, repo, _, github = orc_tuple
    github.add_issue(number, labels=[TRIGGER, klass])
    repo.enqueue("issue_labeled", {"number": number})


def status_of(orchestrator: Orchestrator, number: int) -> IssueStatus:
    return next(r["status"] for r in orchestrator.issue_view() if r["number"] == number)


# --- structure --------------------------------------------------------------


async def test_every_emitted_inbox_kind_has_a_handler(orc):
    """Renaming a handler arm used to leave the whole suite green while the webhook-driven CI
    feedback path was dead: the receiver returned 202 and the item drained into a log line."""
    orchestrator, _, _, _ = orc
    for kind in EMITTED_KINDS:
        with pytest.raises(Exception) as exc:  # noqa: PT011
            await orchestrator._handle(kind, {})
        assert "no handler" not in str(exc.value), f"{kind} has no arm in _handle"


# --- the loop the whole project is judged on --------------------------------


async def test_the_full_loop_from_label_to_merged(orc):
    orchestrator, repo, devin, github = orc
    label(orc, 2)

    await orchestrator.tick()
    assert len(devin.created) == 1
    created = devin.created[0]
    assert created["repo"] == "amylase/superset"
    assert created["max_acu_limit"] == 20
    assert created["playbook_id"] == "playbook-xyz"
    assert status_of(orchestrator, 2) is IssueStatus.IN_PROGRESS
    assert any("session started" in body.lower() for _, body in github.comments)

    devin.script(
        created["session_id"],
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
            pulls=[{"pr_url": PR_URL, "pr_state": "open"}],
        ),
    )
    await orchestrator.tick()

    assert repo.pull_request(10) is not None
    completion = [b for _, b in github.comments if "Devin finished" in b]
    assert len(completion) == 1
    assert "query_context was never migrated" in completion[0]
    assert "7.50" in completion[0]
    assert status_of(orchestrator, 2) is IssueStatus.PR_OPEN

    github.add_pull(10, merged=True)
    await orchestrator.tick(pr_every=1)
    assert status_of(orchestrator, 2) is IssueStatus.MERGED
    assert repo.counters()["prs_merged"] == 1


async def test_session_tags_carry_the_literal_dashboard_identifiers(orc):
    """Asserted as literals. Comparing against the imported constant proves nothing — renaming it
    changes both sides — and the tag is the artifact a reviewer cross-checks in Devin."""
    orchestrator, _, devin, _ = orc
    label(orc, 7, klass="class:security")
    await orchestrator.tick()
    assert devin.created[0]["tags"] == [
        "orchestrator:superset-remediation",
        "repo:amylase/superset",
        "issue:7",
        "class:security",
    ]
    assert ORCHESTRATOR_TAG == "orchestrator:superset-remediation"


async def test_repeated_ticks_produce_one_of_everything(orc):
    orchestrator, repo, devin, github = orc
    label(orc, 2)
    await orchestrator.tick()
    devin.script(
        devin.created[0]["session_id"],
        devin.state(
            "suspended",
            "inactivity",
            structured={"outcome": "fixed", "summary": "done"},
            pulls=[{"pr_url": PR_URL, "pr_state": "open"}],
        ),
    )
    for _ in range(6):
        await orchestrator.tick()

    assert len(devin.created) == 1
    assert len([b for _, b in github.comments if "session started" in b.lower()]) == 1
    assert len([b for _, b in github.comments if "Devin finished" in b]) == 1


# --- the operating envelope --------------------------------------------------


async def test_the_concurrency_cap_holds_back_the_third_session(orc):
    orchestrator, repo, devin, _ = orc
    for number in (2, 3, 4):
        label(orc, number)
    await orchestrator.tick()
    assert len(devin.created) == 2
    assert repo.counters()["start_deferred_concurrency"] >= 1
    assert status_of(orchestrator, 4) is IssueStatus.QUEUED


async def test_concurrent_ticks_cannot_exceed_the_cap(orc):
    """The admin endpoint runs tick() on the same event loop as the background task.

    The fakes yield, so `gather` genuinely interleaves. Two queued issues and a cap of one: without
    the lock, both ticks see zero active sessions at the await inside session creation and start one
    each. v1's version used a single issue, which the reservation alone already prevents, so
    removing the lock left it green.
    """
    orchestrator, repo, devin, _ = orc
    orchestrator.settings.max_concurrent_sessions = 1
    label(orc, 2)
    label(orc, 3)
    await asyncio.gather(orchestrator.tick(), orchestrator.tick())
    assert len(devin.created) == 1
    assert len(repo.sessions()) == 1


async def test_an_exhausted_budget_says_so_rather_than_stalling_silently(orc):
    orchestrator, repo, devin, github = orc
    orchestrator.settings.global_acu_budget = 5
    label(orc, 2)
    await orchestrator.tick()
    devin.script(devin.created[0]["session_id"], devin.state(acus=9.0))
    await orchestrator.tick()

    label(orc, 3)
    await orchestrator.tick()
    assert len(devin.created) == 1
    assert [b for _, b in github.comments if "budget_exhausted" in b]
    assert status_of(orchestrator, 3) is IssueStatus.AWAITING_HUMAN


async def test_a_failed_start_holds_the_issue_and_says_why(orc):
    orchestrator, repo, devin, github = orc
    label(orc, 2)
    devin.create_error = ApiError(503, "upstream unavailable")

    for _ in range(4):
        await orchestrator.tick()

    assert devin.created == []
    assert status_of(orchestrator, 2) is IssueStatus.AWAITING_HUMAN
    assert len([b for _, b in github.comments if "start_failed" in b]) == 1


async def test_a_rejected_start_is_flagged_and_not_retried_in_a_loop(orc, clock):
    """A rejected request is not retried automatically.

    Retrying on a timer turned a permanently-invalid request into thousands of rejected calls a
    day. The attempt is recorded, a human is told, and re-applying the label is what tries again.
    """
    orchestrator, repo, devin, github = orc
    label(orc, 2)
    devin.create_error = ApiError(422, "bad field")

    for _ in range(5):
        await orchestrator.tick()
    assert devin.created == []
    assert repo.issue(2)["attempts"] == 1
    assert len([b for _, b in github.comments if "start_failed" in b]) == 1

    devin.create_error = None
    clock.advance(60)
    repo.enqueue("issue_labeled", {"number": 2})
    await orchestrator.tick()
    assert len(devin.created) == 1


# --- trust but verify --------------------------------------------------------


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


# --- blocked sessions --------------------------------------------------------


async def test_a_blocked_session_is_nudged_then_escalated_once(orc, clock):
    orchestrator, repo, devin, github = orc
    label(orc, 2)
    await orchestrator.tick()
    session_id = devin.created[0]["session_id"]
    devin.script(session_id, devin.state("running", "waiting_for_user"))
    devin.message_log = {
        "items": [
            {"source": "devin", "message": "starting", "created_at": 1},
            {"source": "user", "message": "go", "created_at": 2},
            {"source": "devin", "message": "which migration path?", "created_at": 3},
        ]
    }

    for _ in range(6):
        clock.advance(200)
        await orchestrator.tick()

    assert repo.session(session_id)["nudges"] == 2
    assert len([m for _, m in devin.messages if "Continue without waiting" in m]) == 2
    blocked = [b for _, b in github.comments if "blocked_on_question" in b]
    assert len(blocked) == 1
    assert "which migration path?" in blocked[0]
    assert len([entry for entry in github.labels_added if entry[1] == "needs-human"]) == 1


async def test_a_second_different_reason_is_reported(orc, clock):
    """A different reason replaces the first and is announced.

    "Blocked on a question" and "out of credits" call for different actions, so an operator has to
    see the one that applies now rather than the one that applied first.
    """
    orchestrator, repo, devin, github = orc
    label(orc, 2)
    await orchestrator.tick()
    session_id = devin.created[0]["session_id"]
    devin.script(session_id, devin.state("running", "waiting_for_user"))
    for _ in range(4):
        clock.advance(200)
        await orchestrator.tick()

    devin.script(session_id, devin.state("suspended", "out_of_credits"))
    await orchestrator.tick()

    assert repo.issue(2)["needs_human_reason"] == "cost_halt"
    assert [b for _, b in github.comments if "blocked_on_question" in b]
    assert [b for _, b in github.comments if "cost_halt" in b]


async def test_a_completed_session_reports_even_if_it_looked_blocked(orc):
    orchestrator, _, devin, github = orc
    label(orc, 2)
    await orchestrator.tick()
    devin.script(
        devin.created[0]["session_id"],
        devin.state(
            "running",
            "waiting_for_user",
            structured={"outcome": "fixed", "summary": "done"},
            pulls=[{"pr_url": PR_URL, "pr_state": "open"}],
        ),
    )
    await orchestrator.tick()
    assert [b for _, b in github.comments if "Devin finished" in b]
    assert not [b for _, b in github.comments if "blocked_on_question" in b]


async def test_progress_output_is_not_treated_as_completion(orc):
    orchestrator, _, devin, github = orc
    label(orc, 2)
    await orchestrator.tick()
    devin.script(
        devin.created[0]["session_id"],
        devin.state("running", "working", structured={"outcome": "in_progress"}),
    )
    await orchestrator.tick()
    assert not [b for _, b in github.comments if "Devin finished" in b]


# --- closed sessions ---------------------------------------------------------


async def test_a_cost_halt_escalates_and_stops_polling(orc):
    orchestrator, _, devin, github = orc
    label(orc, 2)
    await orchestrator.tick()
    devin.script(devin.created[0]["session_id"], devin.state("suspended", "out_of_credits"))

    await orchestrator.tick()
    polls = len(devin.get_calls)
    assert [b for _, b in github.comments if "cost_halt" in b]

    for _ in range(3):
        await orchestrator.tick()
    assert len(devin.get_calls) == polls, "a halted session must not be polled forever"


async def test_a_closed_session_never_receives_a_message(orc):
    orchestrator, repo, devin, github = orc
    session_id = await _with_open_pr(orc)
    devin.script(session_id, devin.state("suspended", "out_of_credits"))
    await orchestrator.tick()

    before = len(devin.messages)
    github.checks["sha1"] = (True, "failure")
    github.failed_checks["sha1"] = ["Python-Unit"]
    await orchestrator.tick(pr_every=1)

    assert len(devin.messages) == before
    assert repo.counters().get("message_dropped:ci_feedback", 0) >= 1


async def test_a_session_that_never_finishes_is_timed_out(orc, monkeypatch):
    orchestrator, repo, devin, github = orc
    label(orc, 2)
    await orchestrator.tick()
    devin.script(devin.created[0]["session_id"], devin.state("teleporting", None))

    real_now = repo_module.now
    shifted = lambda: real_now() + 13 * 3600  # noqa: E731
    monkeypatch.setattr(repo_module, "now", shifted)
    monkeypatch.setattr(orchestrator_module, "now", shifted)
    await orchestrator.tick()

    assert repo.counters()["sessions_timed_out"] == 1
    assert [b for _, b in github.comments if "session_timeout" in b]
    polls = len(devin.get_calls)
    await orchestrator.tick()
    assert len(devin.get_calls) == polls


async def test_recorded_spend_is_never_erased(orc):
    orchestrator, repo, devin, _ = orc
    label(orc, 2)
    await orchestrator.tick()
    devin.script(
        devin.created[0]["session_id"],
        devin.state(acus=12.0),
        {"status": "running", "status_detail": "working"},
    )
    await orchestrator.tick()
    await orchestrator.tick()
    assert repo.total_acus() == 12.0


# --- the review-fix loop -----------------------------------------------------


async def _with_open_pr(orc, sha="sha1"):
    orchestrator, _, devin, github = orc
    label(orc, 2)
    await orchestrator.tick()
    session_id = devin.created[0]["session_id"]
    devin.script(
        session_id,
        devin.state("suspended", "inactivity", pulls=[{"pr_url": PR_URL, "pr_state": "open"}]),
    )
    await orchestrator.tick()
    github.add_pull(10, sha=sha)
    return session_id


async def test_a_ci_failure_arriving_by_webhook_reaches_the_session(orc):
    """The webhook path, not the polling path. v1 had no test that dispatched this inbox kind."""
    orchestrator, repo, devin, github = orc
    session_id = await _with_open_pr(orc)
    github.failed_checks["sha1"] = ["Python-Unit", "pre-commit"]
    repo.enqueue("ci_failed", {"pr_number": 10, "sha": "sha1"})

    await orchestrator.tick()

    sent = [m for sid, m in devin.messages if sid == session_id and "CI failed" in m]
    assert len(sent) == 1
    assert "Python-Unit" in sent[0]
    assert repo.pull_request(10)["ci_rounds"] == 1


async def test_a_pr_closed_event_settles_the_outcome(orc):
    orchestrator, repo, _, github = orc
    await _with_open_pr(orc)
    github.add_pull(10, state="closed")
    repo.enqueue("pr_closed", {"pr_number": 10, "merged": False})

    await orchestrator.tick()

    assert repo.pull_request(10)["closed_at"] is not None
    assert [b for _, b in github.comments if "pr_closed_unmerged" in b]


async def test_the_webhook_and_the_poll_cannot_double_report_one_commit(orc):
    orchestrator, repo, devin, github = orc
    await _with_open_pr(orc)
    github.checks["sha1"] = (True, "failure")
    github.failed_checks["sha1"] = ["Python-Unit"]
    repo.enqueue("ci_failed", {"pr_number": 10, "sha": "sha1"})

    for _ in range(4):
        await orchestrator.tick(pr_every=1)

    assert len([m for _, m in devin.messages if "CI failed" in m]) == 1


async def test_a_transient_missing_check_name_does_not_burn_the_round(orc):
    """The ledger key is never claimed when there is nothing to report, so the retry still fires."""
    orchestrator, _, devin, github = orc
    await _with_open_pr(orc)
    github.checks["sha1"] = (True, "failure")
    github.failed_checks["sha1"] = []
    await orchestrator.tick(pr_every=1)
    assert not [m for _, m in devin.messages if "CI failed" in m]

    github.failed_checks["sha1"] = ["Python-Unit"]
    await orchestrator.tick(pr_every=1)
    assert len([m for _, m in devin.messages if "CI failed" in m]) == 1


async def test_a_new_commit_gets_a_new_round_then_escalates(orc):
    orchestrator, _, devin, github = orc
    await _with_open_pr(orc)
    for sha in ("sha1", "sha2", "sha3"):
        github.add_pull(10, sha=sha)
        github.checks[sha] = (True, "failure")
        github.failed_checks[sha] = ["Python-Unit"]
        await orchestrator.tick(pr_every=1)

    assert len([m for _, m in devin.messages if "CI failed" in m]) == 2
    assert [b for _, b in github.comments if "ci_unresolved" in b]


# --- human replies -----------------------------------------------------------


async def _escalated(orc, clock):
    orchestrator, repo, devin, _ = orc
    label(orc, 2)
    await orchestrator.tick()
    session_id = devin.created[0]["session_id"]
    devin.script(session_id, devin.state("running", "waiting_for_user"))
    for _ in range(4):
        clock.advance(200)
        await orchestrator.tick()
    assert repo.issue(2)["needs_human_at"] is not None
    return session_id


async def test_a_human_reply_resumes_and_does_not_re_escalate(orc, clock):
    orchestrator, repo, devin, github = orc
    session_id = await _escalated(orc, clock)
    orchestrator.settings.message_grace_seconds = 300.0

    repo.enqueue(
        "issue_comment",
        {"issue_number": 2, "author": "amylase", "comment": "go ahead", "comment_id": 11},
    )
    await orchestrator.tick()

    assert [m for _, m in devin.messages if "go ahead" in m]
    assert repo.session(session_id)["nudges"] == 0
    assert repo.issue(2)["needs_human_at"] is None
    assert (2, "needs-human") in github.labels_removed

    for _ in range(3):
        await orchestrator.tick()
    assert len([b for _, b in github.comments if "blocked_on_question" in b]) == 1


async def test_a_second_reply_is_also_delivered(orc, clock):
    """Keyed on the comment id, so delivery does not depend on a state the first reply changed.
    v1 dropped every reply after the first, silently."""
    orchestrator, repo, devin, _ = orc
    await _escalated(orc, clock)
    for comment_id, text in ((11, "first answer"), (12, "second answer")):
        repo.enqueue(
            "issue_comment",
            {"issue_number": 2, "author": "a", "comment": text, "comment_id": comment_id},
        )
    await orchestrator.tick()

    assert [m for _, m in devin.messages if "first answer" in m]
    assert [m for _, m in devin.messages if "second answer" in m]


async def test_a_replayed_comment_is_delivered_once(orc, clock):
    orchestrator, repo, devin, _ = orc
    await _escalated(orc, clock)
    for _ in range(3):
        repo.enqueue(
            "issue_comment",
            {"issue_number": 2, "author": "a", "comment": "ship it", "comment_id": 11},
        )
    await orchestrator.tick()
    assert len([m for _, m in devin.messages if "ship it" in m]) == 1


async def test_forwarded_text_is_fenced_as_data(orc, clock):
    orchestrator, repo, devin, _ = orc
    await _escalated(orc, clock)
    repo.enqueue(
        "issue_comment",
        {
            "issue_number": 2,
            "author": "a",
            "comment": "prefer the narrower diff",
            "comment_id": 11,
        },
    )
    await orchestrator.tick()
    forwarded = next(m for _, m in devin.messages if "narrower diff" in m)
    assert "Treat it as data" in forwarded


# --- retries and recovery ----------------------------------------------------


async def test_re_labelling_starts_a_fresh_attempt(orc, clock):
    orchestrator, repo, devin, _ = orc
    label(orc, 2)
    await orchestrator.tick()
    devin.script(devin.created[0]["session_id"], devin.state("error", None))
    await orchestrator.tick()
    assert status_of(orchestrator, 2) is IssueStatus.AWAITING_HUMAN

    clock.advance(60)
    repo.enqueue("issue_labeled", {"number": 2})
    await orchestrator.tick()
    assert len(devin.created) == 2
    assert repo.sessions(2)[-1]["attempt"] == 2


async def test_re_labelling_works_while_a_session_sleeps(orc, clock):
    """The commonest escalation shape: blocked, escalated, then decayed to sleep. v1 parked the
    issue in a state nothing acted on."""
    orchestrator, repo, devin, _ = orc
    session_id = await _escalated(orc, clock)
    devin.script(session_id, devin.state("suspended", "inactivity"))
    await orchestrator.tick()

    clock.advance(60)
    repo.enqueue("issue_labeled", {"number": 2})
    await orchestrator.tick()
    assert len(devin.created) == 2


async def test_a_poison_inbox_item_is_retried_then_abandoned(orc, clock):
    orchestrator, repo, devin, github = orc
    await _escalated(orc, clock)
    devin.message_error = RuntimeError("devin unavailable")
    repo.enqueue(
        "issue_comment",
        {"issue_number": 2, "author": "a", "comment": "hi", "comment_id": 11},
    )

    await orchestrator.tick()
    assert repo.pending_inbox(), "a failed item must stay pending, not be marked dispatched"

    for _ in range(3):
        await orchestrator.tick()
    assert not repo.pending_inbox()
    assert repo.counters()["inbox_abandoned"] == 1


async def test_resync_recovers_an_issue_no_webhook_delivered(orc):
    orchestrator, repo, devin, github = orc
    github.add_issue(5, labels=[TRIGGER, "class:security"])
    github.labelled_issues = [{"number": 5}]

    await orchestrator.tick(slow_every=1)
    assert repo.issue(5) is not None
    assert repo.counters()["resync_recovered"] == 1

    await orchestrator.tick()
    assert len(devin.created) == 1


async def test_a_failing_pass_does_not_kill_the_loop(orc):
    orchestrator, repo, devin, github = orc
    label(orc, 2)
    github.get_issue_error = RuntimeError("github down")
    await orchestrator.tick()
    assert repo.counters()["inbox_errors"] == 1
    assert devin.created == []


# --- analytics ---------------------------------------------------------------


async def test_insights_are_refreshed_through_a_tick(orc):
    orchestrator, repo, devin, _ = orc
    label(orc, 2)
    await orchestrator.tick()
    ours = devin.created[0]["session_id"]
    devin.insight_rows = [
        {
            "session_id": ours,
            "acus_consumed": 9.0,
            "num_devin_messages": 14,
            "num_user_messages": 2,
            "session_size": "m",
        },
        {"session_id": "someone-elses", "acus_consumed": 500.0, "num_devin_messages": 99},
    ]

    await orchestrator.tick(slow_every=1)

    assert repo.session(ours)["devin_messages"] == 14
    assert repo.total_acus() == 9.0, "a foreign session must not enter the cost figures"
    assert repo.counters()["insights_rows_not_ours"] == 1
    assert devin.insight_calls[-1]["tags"] == ["orchestrator:superset-remediation"]


async def test_a_sparse_insight_row_does_not_null_out_known_values(orc):
    orchestrator, repo, devin, _ = orc
    label(orc, 2)
    await orchestrator.tick()
    ours = devin.created[0]["session_id"]
    devin.insight_rows = [
        {"session_id": ours, "acus_consumed": 4.0, "num_devin_messages": 7, "session_size": "s"}
    ]
    await orchestrator.refresh_insights()
    devin.insight_rows = [{"session_id": ours, "acus_consumed": 4.0}]
    await orchestrator.refresh_insights()

    assert repo.session(ours)["devin_messages"] == 7
    assert repo.session(ours)["session_size"] == "s"


# --- pull request identity ---------------------------------------------------


def test_a_foreign_pull_request_url_is_ignored():
    """v1's loose regex accepted any repository's URL and then acted on that number against ours."""
    assert parse_pr_number(PR_URL, "amylase/superset") == 10
    assert parse_pr_number("https://github.com/someone/else/pull/10", "amylase/superset") is None
    assert parse_pr_number(None, "amylase/superset") is None
