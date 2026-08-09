"""Event routing. Pure function, so this is a table of cases rather than a fixture assembly."""

from __future__ import annotations

import pytest

from app.webhooks.handlers import MAX_FORWARDED_CHARS, to_intent

LABEL = "devin-fix"


def test_labeled_with_the_trigger_label_starts_work():
    intent = to_intent(
        "issues",
        {"action": "labeled", "label": {"name": LABEL}, "issue": {"number": 5}},
        trigger_label=LABEL,
    )
    assert intent == ("issue_labeled", {"number": 5})


@pytest.mark.parametrize("action", ["edited", "reopened", "unlabeled", "closed", "assigned"])
def test_other_issue_actions_are_ignored(action):
    payload = {"action": action, "label": {"name": LABEL}, "issue": {"number": 5}}
    assert to_intent("issues", payload, trigger_label=LABEL) is None


def test_failed_ci_is_handed_back():
    payload = {
        "action": "completed",
        "workflow_run": {
            "conclusion": "failure",
            "head_sha": "abc123",
            "name": "Python-Unit",
            "pull_requests": [{"number": 11}],
        },
    }
    kind, data = to_intent("workflow_run", payload, trigger_label=LABEL)
    assert kind == "ci_failed"
    assert data["pr_number"] == 11
    assert data["sha"] == "abc123"


def test_successful_ci_is_not_handed_back():
    payload = {
        "action": "completed",
        "workflow_run": {"conclusion": "success", "pull_requests": [{"number": 11}]},
    }
    assert to_intent("workflow_run", payload, trigger_label=LABEL) is None


def test_ci_failure_without_an_associated_pr_is_ignored():
    payload = {
        "action": "completed",
        "workflow_run": {"conclusion": "failure", "pull_requests": []},
    }
    assert to_intent("workflow_run", payload, trigger_label=LABEL) is None


def issue_comment(body="use the conservative option", *, assoc="OWNER", login="amylase", **extra):
    return {
        "action": "created",
        "issue": {"number": 5},
        "comment": {"body": body, "user": {"login": login, **extra}, "author_association": assoc},
    }


def review_comment(body="this leaks a session", *, assoc="COLLABORATOR", login="reviewer", **extra):
    return {
        "action": "created",
        "pull_request": {"number": 12},
        "comment": {"body": body, "user": {"login": login, **extra}, "author_association": assoc},
    }


def test_human_issue_comment_is_forwarded():
    kind, data = to_intent(
        "issue_comment", issue_comment("  use the conservative option  "), trigger_label=LABEL
    )
    assert kind == "issue_comment"
    assert data == {
        "issue_number": 5,
        "author": "amylase",
        "comment": "use the conservative option",
    }


def test_bot_comments_are_not_forwarded():
    """The orchestrator writes back to the issue itself; forwarding that would loop."""
    assert (
        to_intent(
            "issue_comment", issue_comment("session started", type="Bot"), trigger_label=LABEL
        )
        is None
    )


def test_review_comment_is_forwarded():
    kind, data = to_intent("pull_request_review_comment", review_comment(), trigger_label=LABEL)
    assert kind == "review_comment"
    assert data["pr_number"] == 12 and data["reviewer"] == "reviewer"


# --- the trust gate ---------------------------------------------------------
#
# Anything forwarded reaches an agent with a checked-out working tree and push rights, so these
# paths are an instruction channel. The fork is public: without the gate, any account could comment
# on an open remediation pull request and have that text delivered to the agent.


@pytest.mark.parametrize(
    "assoc", ["NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "MANNEQUIN", None]
)
def test_untrusted_authors_cannot_reach_the_agent(assoc):
    assert to_intent("issue_comment", issue_comment(assoc=assoc), trigger_label=LABEL) is None
    assert (
        to_intent("pull_request_review_comment", review_comment(assoc=assoc), trigger_label=LABEL)
        is None
    )


@pytest.mark.parametrize("assoc", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_trusted_authors_are_forwarded(assoc):
    assert to_intent("issue_comment", issue_comment(assoc=assoc), trigger_label=LABEL) is not None
    assert (
        to_intent("pull_request_review_comment", review_comment(assoc=assoc), trigger_label=LABEL)
        is not None
    )


def test_forwarded_text_is_truncated():
    """A very long comment is a way to push the real task out of the agent's attention."""
    _, data = to_intent("issue_comment", issue_comment("x" * 20_000), trigger_label=LABEL)
    assert len(data["comment"]) == MAX_FORWARDED_CHARS


def test_empty_review_comments_are_not_forwarded():
    assert (
        to_intent("pull_request_review_comment", review_comment("   "), trigger_label=LABEL) is None
    )


@pytest.mark.parametrize("conclusion", ["startup_failure", "cancelled", "action_required", "stale"])
def test_other_failing_workflow_conclusions_are_handed_back(conclusion):
    """`startup_failure` in particular is what a malformed workflow file produces."""
    payload = {
        "action": "completed",
        "workflow_run": {
            "conclusion": conclusion,
            "head_sha": "abc",
            "pull_requests": [{"number": 11}],
        },
    }
    assert to_intent("workflow_run", payload, trigger_label=LABEL)[0] == "ci_failed"


def test_closed_pull_request_settles_the_outcome():
    payload = {"action": "closed", "pull_request": {"number": 12, "merged": True}}
    assert to_intent("pull_request", payload, trigger_label=LABEL) == (
        "pr_closed",
        {"pr_number": 12, "merged": True},
    )


@pytest.mark.parametrize("event", ["push", "star", "fork", "check_suite"])
def test_unhandled_events_are_ignored(event):
    assert to_intent(event, {"action": "created"}, trigger_label=LABEL) is None
