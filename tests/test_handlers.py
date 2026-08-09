"""Event routing and the trust boundary.

A pure function, so this is a table of cases. The identity check matters most: the orchestrator
writes with a personal access token, so its own comments arrive as an ordinary user with
``author_association: OWNER``. A bot filter does not match them, so without an identity check the
trust gate whitelists the system's own escalation comments — they are forwarded to Devin as human
answers, the nudge budget resets, and it escalates again, indefinitely.
"""

from __future__ import annotations

import pytest

from app.webhooks.handlers import (
    EMITTED_KINDS,
    MAX_FORWARDED_CHARS,
    may_reach_the_agent,
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


def intent(event, payload, own_login=OWN):
    return to_intent(event, payload, trigger_label=LABEL, own_login=own_login)


# --- identity ----------------------------------------------------------------


def test_our_own_comment_is_never_forwarded():
    """The infinite escalation loop: our own notification comes back as OWNER."""
    payload = issue_comment("🙋 Human input needed", login=OWN, assoc="OWNER")
    assert may_reach_the_agent(payload["comment"], own_login=OWN) is False
    assert intent("issue_comment", payload) is None


def test_the_identity_check_is_case_insensitive():
    assert (
        may_reach_the_agent(issue_comment(login="Orchestrator-Bot")["comment"], own_login=OWN)
        is False
    )


def test_a_bot_is_still_excluded():
    assert intent("issue_comment", issue_comment(login="dependabot", type="Bot")) is None


def test_an_unknown_identity_refuses_every_comment():
    """Fails closed.

    If we do not know which login is ours, our own comments cannot be told from a maintainer's.
    Forwarding is disabled until the identity resolves rather than risking the loop.
    """
    assert may_reach_the_agent(issue_comment()["comment"], own_login=None) is False
    assert intent("issue_comment", issue_comment(), own_login=None) is None
    assert intent("pull_request_review_comment", review_comment(), own_login=None) is None


def test_an_unknown_identity_still_allows_the_trigger_label():
    """Labels are not text handed to the agent, so the gate does not apply to them."""
    payload = {"action": "labeled", "label": {"name": LABEL}, "issue": {"number": 5}}
    assert intent("issues", payload, own_login=None) == ("issue_labeled", {"number": 5})


# --- the trust gate ----------------------------------------------------------


@pytest.mark.parametrize(
    "assoc", ["NONE", "CONTRIBUTOR", "FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR", "MANNEQUIN", None]
)
def test_untrusted_authors_cannot_reach_the_agent(assoc):
    assert intent("issue_comment", issue_comment(assoc=assoc)) is None
    assert intent("pull_request_review_comment", review_comment(assoc=assoc)) is None


@pytest.mark.parametrize("assoc", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_trusted_authors_are_forwarded(assoc):
    assert intent("issue_comment", issue_comment(assoc=assoc)) is not None
    assert intent("pull_request_review_comment", review_comment(assoc=assoc)) is not None


def test_forwarded_text_is_truncated():
    _, data = intent("issue_comment", issue_comment("x" * 20_000))
    assert len(data["comment"]) == MAX_FORWARDED_CHARS


def test_a_comment_carries_its_id_so_delivery_can_be_deduped():
    _, data = intent("issue_comment", issue_comment())
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
    kind, data = intent("issues", payload)
    assert (kind, data) == ("issue_labeled", {"number": 5})
    assert isinstance(data["number"], int)


@pytest.mark.parametrize("sha", ["../../../user", "not a sha", "", None, 123])
def test_a_malformed_commit_sha_is_dropped(sha):
    """The sha reaches `/commits/{sha}/check-runs`, so it gets the same treatment as a number."""
    payload = {
        "action": "completed",
        "workflow_run": {
            "conclusion": "failure",
            "head_sha": sha,
            "pull_requests": [{"number": 1}],
        },
    }
    _, data = intent("workflow_run", payload)
    assert data["sha"] is None


def test_a_real_sha_survives():
    payload = {
        "action": "completed",
        "workflow_run": {
            "conclusion": "failure",
            "head_sha": "3b164e4270860ac07223d4df1a60ca7b56312362",
            "pull_requests": [{"number": 1}],
        },
    }
    _, data = intent("workflow_run", payload)
    assert data["sha"] == "3b164e4270860ac07223d4df1a60ca7b56312362"


# --- routing -----------------------------------------------------------------


def test_labeled_with_the_trigger_label_starts_work():
    payload = {"action": "labeled", "label": {"name": LABEL}, "issue": {"number": 5}}
    assert intent("issues", payload) == ("issue_labeled", {"number": 5})


@pytest.mark.parametrize("action", ["edited", "reopened", "unlabeled", "closed", "assigned"])
def test_other_issue_actions_are_ignored(action):
    payload = {"action": action, "label": {"name": LABEL}, "issue": {"number": 5}}
    assert intent("issues", payload) is None


def test_a_different_label_does_not_trigger():
    payload = {"action": "labeled", "label": {"name": "documentation"}, "issue": {"number": 5}}
    assert intent("issues", payload) is None


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "cancelled", "action_required"])
def test_failing_conclusions_are_handed_back(conclusion):
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
    assert intent("pull_request", payload) == ("pr_closed", {"pr_number": 12, "merged": True})


@pytest.mark.parametrize("event", ["push", "star", "fork", "check_suite"])
def test_unhandled_events_are_ignored(event):
    assert intent(event, {"action": "created"}) is None


def test_the_emitted_kinds_list_matches_what_the_router_can_produce():
    """`EMITTED_KINDS` is what the orchestrator asserts it handles, so it must stay truthful.

    Together with `test_every_emitted_inbox_kind_has_a_handler` this closes the loop: emptying the
    set makes the structural test vacuous, and this test then fails.
    """
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
