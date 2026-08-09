"""Event routing. Pure function, so this is a table of cases rather than a fixture assembly."""

from __future__ import annotations

import pytest

from app.webhooks.handlers import to_intent

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


def test_human_issue_comment_is_forwarded():
    payload = {
        "action": "created",
        "issue": {"number": 5},
        "comment": {"body": "  use the conservative option  ", "user": {"login": "amylase"}},
    }
    kind, data = to_intent("issue_comment", payload, trigger_label=LABEL)
    assert kind == "issue_comment"
    assert data == {
        "issue_number": 5,
        "author": "amylase",
        "comment": "use the conservative option",
    }


def test_bot_comments_are_not_forwarded():
    """The orchestrator writes back to the issue itself; forwarding that would loop."""
    payload = {
        "action": "created",
        "issue": {"number": 5},
        "comment": {"body": "session started", "user": {"login": "bot", "type": "Bot"}},
    }
    assert to_intent("issue_comment", payload, trigger_label=LABEL) is None


def test_review_comment_is_forwarded():
    payload = {
        "action": "created",
        "pull_request": {"number": 12},
        "comment": {"body": "this leaks a session", "user": {"login": "reviewer"}},
    }
    kind, data = to_intent("pull_request_review_comment", payload, trigger_label=LABEL)
    assert kind == "review_comment"
    assert data["pr_number"] == 12 and data["reviewer"] == "reviewer"


def test_closed_pull_request_settles_the_outcome():
    payload = {"action": "closed", "pull_request": {"number": 12, "merged": True}}
    assert to_intent("pull_request", payload, trigger_label=LABEL) == (
        "pr_closed",
        {"pr_number": 12, "merged": True},
    )


@pytest.mark.parametrize("event", ["push", "star", "fork", "check_suite"])
def test_unhandled_events_are_ignored(event):
    assert to_intent(event, {"action": "created"}, trigger_label=LABEL) is None
