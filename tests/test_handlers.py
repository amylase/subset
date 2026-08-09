"""Event routing and the trust boundary.

A pure function, so this is a table of cases. The identity check is the one that matters most: the
orchestrator writes with a personal access token, so its own comments arrive as an ordinary user
with ``author_association: OWNER``. v1's bot filter did not match them, the trust gate whitelisted
them, and the system's own escalation comments were forwarded to Devin as human answers — resetting
the nudge budget and producing another escalation, forever.
"""

from __future__ import annotations

import pytest

from app.webhooks.handlers import (
    EMITTED_KINDS,
    MAX_FORWARDED_CHARS,
    Provenance,
    classify,
    to_intent,
)

LABEL = "devin-fix"
OWN = "orchestrator-bot"


def issue_comment(body="use the conservative option", *, assoc="OWNER", login="amylase", **user):
    return {
        "action": "created",
        "issue": {"number": 5},
        "comment": {
            "id": 900,
            "body": body,
            "user": {"login": login, **user},
            "author_association": assoc,
        },
    }


def review_comment(body="this leaks a session", *, assoc="COLLABORATOR", login="rev", **user):
    return {
        "action": "created",
        "pull_request": {"number": 12},
        "comment": {
            "id": 901,
            "body": body,
            "user": {"login": login, **user},
            "author_association": assoc,
        },
    }


def intent(event, payload):
    return to_intent(event, payload, trigger_label=LABEL, own_login=OWN)


# --- identity ----------------------------------------------------------------


def test_our_own_comment_is_never_forwarded():
    """The infinite escalation loop: our own notification comes back as OWNER."""
    payload = issue_comment("🙋 Human input needed", login=OWN, assoc="OWNER")
    assert classify(payload["comment"], own_login=OWN) is Provenance.SELF
    assert intent("issue_comment", payload) is None


def test_the_identity_check_is_case_insensitive():
    payload = issue_comment(login="Orchestrator-Bot")
    assert classify(payload["comment"], own_login=OWN) is Provenance.SELF


def test_a_bot_is_still_excluded():
    assert intent("issue_comment", issue_comment(login="dependabot", type="Bot")) is None


def test_without_a_known_identity_a_trusted_author_still_passes():
    """Degrades to v1 behaviour rather than refusing to work; startup logs a warning."""
    payload = issue_comment(login=OWN)
    assert to_intent("issue_comment", payload, trigger_label=LABEL, own_login=None) is not None


# --- the trust gate ----------------------------------------------------------


@pytest.mark.parametrize(
    "assoc", ["NONE", "CONTRIBUTOR", "FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR", "MANNEQUIN", None]
)
def test_untrusted_authors_cannot_reach_the_agent(assoc):
    assert intent("issue_comment", issue_comment(assoc=assoc)) is None
    assert intent("pull_request_review_comment", review_comment(assoc=assoc)) is None


@pytest.mark.parametrize("assoc", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_trusted_authors_are_forwarded(assoc):
    for event, payload in (
        ("issue_comment", issue_comment(assoc=assoc)),
        ("pull_request_review_comment", review_comment(assoc=assoc)),
    ):
        result = intent(event, payload)
        assert result is not None
        assert result[2] is Provenance.TRUSTED


def test_forwarded_text_is_truncated():
    _, data, _ = intent("issue_comment", issue_comment("x" * 20_000))
    assert len(data["comment"]) == MAX_FORWARDED_CHARS


def test_a_comment_carries_its_id_so_delivery_can_be_deduped():
    _, data, _ = intent("issue_comment", issue_comment())
    assert data["comment_id"] == 900


def test_empty_comments_are_not_forwarded():
    assert intent("pull_request_review_comment", review_comment("   ")) is None


# --- identifier coercion -----------------------------------------------------


def test_a_non_numeric_issue_number_is_rejected():
    """httpx normalises dot segments, so a string here could rewrite an API path and reach an
    unrelated GitHub endpoint with the token."""
    payload = {
        "action": "labeled",
        "label": {"name": LABEL},
        "issue": {"number": "1/comments/../../../../user"},
    }
    assert intent("issues", payload) is None


def test_a_numeric_string_is_coerced():
    payload = {"action": "labeled", "label": {"name": LABEL}, "issue": {"number": "5"}}
    kind, data, _ = intent("issues", payload)
    assert (kind, data) == ("issue_labeled", {"number": 5})
    assert isinstance(data["number"], int)


# --- routing -----------------------------------------------------------------


def test_labeled_with_the_trigger_label_starts_work():
    payload = {"action": "labeled", "label": {"name": LABEL}, "issue": {"number": 5}}
    assert intent("issues", payload) == ("issue_labeled", {"number": 5}, Provenance.SYSTEM)


@pytest.mark.parametrize("action", ["edited", "reopened", "unlabeled", "closed", "assigned"])
def test_other_issue_actions_are_ignored(action):
    payload = {"action": action, "label": {"name": LABEL}, "issue": {"number": 5}}
    assert intent("issues", payload) is None


def test_a_different_label_does_not_trigger():
    payload = {"action": "labeled", "label": {"name": "documentation"}, "issue": {"number": 5}}
    assert intent("issues", payload) is None


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
    kind, data, _ = intent("workflow_run", payload)
    assert kind == "ci_failed"
    assert data["pr_number"] == 11 and data["sha"] == "abc123"


@pytest.mark.parametrize("conclusion", ["startup_failure", "cancelled", "action_required", "stale"])
def test_other_failing_conclusions_are_handed_back(conclusion):
    """`startup_failure` is what a malformed workflow file produces."""
    payload = {
        "action": "completed",
        "workflow_run": {"conclusion": conclusion, "pull_requests": [{"number": 11}]},
    }
    assert intent("workflow_run", payload)[0] == "ci_failed"


def test_successful_ci_is_not_handed_back():
    payload = {
        "action": "completed",
        "workflow_run": {"conclusion": "success", "pull_requests": [{"number": 11}]},
    }
    assert intent("workflow_run", payload) is None


def test_ci_failure_without_an_associated_pr_is_ignored():
    payload = {
        "action": "completed",
        "workflow_run": {"conclusion": "failure", "pull_requests": []},
    }
    assert intent("workflow_run", payload) is None


def test_closed_pull_request_settles_the_outcome():
    payload = {"action": "closed", "pull_request": {"number": 12, "merged": True}}
    kind, data, _ = intent("pull_request", payload)
    assert (kind, data) == ("pr_closed", {"pr_number": 12, "merged": True})


@pytest.mark.parametrize("event", ["push", "star", "fork", "check_suite"])
def test_unhandled_events_are_ignored(event):
    assert intent(event, {"action": "created"}) is None


def test_the_emitted_kinds_list_matches_what_the_router_can_produce():
    """`EMITTED_KINDS` is what the orchestrator asserts it handles, so it must stay truthful."""
    produced = set()
    for event, payload in (
        ("issues", {"action": "labeled", "label": {"name": LABEL}, "issue": {"number": 1}}),
        ("issue_comment", issue_comment()),
        ("pull_request_review_comment", review_comment()),
        (
            "workflow_run",
            {
                "action": "completed",
                "workflow_run": {"conclusion": "failure", "pull_requests": [{"number": 1}]},
            },
        ),
        ("pull_request", {"action": "closed", "pull_request": {"number": 1, "merged": False}}),
    ):
        result = intent(event, payload)
        assert result is not None, event
        produced.add(result[0])
    assert produced == set(EMITTED_KINDS)
