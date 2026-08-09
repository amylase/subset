"""Turning GitHub events into orchestrator intent.

A pure function, so the whole routing and trust surface is testable without a server, a database or
a network.

Everything the orchestrator later forwards to a Devin session reaches an agent holding a
checked-out working tree and push rights on a public repository, so the comment paths are an
instruction channel rather than a comment feed. :func:`may_reach_the_agent` is the gate, and it
fails closed — including when our own identity is unknown.
"""

from __future__ import annotations

import re
from typing import Any

from app.clients.github import FAILING_CONCLUSIONS

#: Associations that imply repository trust. Note this is repository *access*: `COLLABORATOR`
#: includes read and triage collaborators, not only those who can write.
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

#: Untrusted text is truncated before it is forwarded. A very long comment is not a useful
#: instruction, and it is a way to push the real task out of the agent's attention.
MAX_FORWARDED_CHARS = 4000

Intent = tuple[str, dict[str, Any]] | None


def may_reach_the_agent(comment: dict[str, Any], *, own_login: str | None) -> bool:
    """Whether this comment's text may be forwarded to a Devin session.

    Fails closed. Identity is checked before association and an unknown identity is a refusal:
    the orchestrator writes with a personal access token, so its own comments arrive as an ordinary
    user with ``author_association: OWNER``. If we do not know which login is ours we cannot tell
    them apart from a maintainer's, and forwarding our own escalation comment back to Devin as a
    human answer resets the nudge budget and escalates again — indefinitely.
    """
    if not own_login:
        return False
    user = comment.get("user") or {}
    login = user.get("login")
    if login and login.lower() == own_login.lower():
        return False
    if user.get("type") == "Bot":
        return False
    return comment.get("author_association") in TRUSTED_ASSOCIATIONS


def _sha(value: Any) -> str | None:
    """Coerce a commit sha that will end up in an API path, like :func:`_number`."""
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{7,40}", value) else None


def _number(value: Any) -> int | None:
    """Coerce an identifier that will end up in an API path.

    v1 passed these straight into f-string URLs. httpx normalises dot segments, so a string value
    could rewrite the request path and reach unrelated GitHub endpoints with the token.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def to_intent(
    event: str, payload: dict[str, Any], *, trigger_label: str, own_login: str | None = None
) -> Intent:
    match event:
        case "issues":
            return _issues(payload, trigger_label)
        case "issue_comment":
            return _issue_comment(payload, own_login)
        case "workflow_run":
            return _workflow_run(payload)
        case "pull_request_review_comment":
            return _review_comment(payload, own_login)
        case "pull_request":
            return _pull_request(payload)
        case _:
            return None


def _issues(payload: dict[str, Any], trigger_label: str) -> Intent:
    if payload.get("action") != "labeled":
        return None
    if (payload.get("label") or {}).get("name") != trigger_label:
        return None
    number = _number((payload.get("issue") or {}).get("number"))
    if number is None:
        return None
    return "issue_labeled", {"number": number}


def _comment_fields(
    payload: dict[str, Any], own_login: str | None
) -> tuple[dict[str, Any], str, int] | None:
    if payload.get("action") != "created":
        return None
    comment = payload.get("comment") or {}
    if not may_reach_the_agent(comment, own_login=own_login):
        return None
    body = (comment.get("body") or "").strip()
    comment_id = _number(comment.get("id"))
    if not body or comment_id is None:
        return None
    return comment, body[:MAX_FORWARDED_CHARS], comment_id


def _issue_comment(payload: dict[str, Any], own_login: str | None) -> Intent:
    fields = _comment_fields(payload, own_login)
    if fields is None:
        return None
    comment, body, comment_id = fields
    number = _number((payload.get("issue") or {}).get("number"))
    if number is None:
        return None
    return (
        "issue_comment",
        {
            "issue_number": number,
            "author": (comment.get("user") or {}).get("login", "unknown"),
            "comment": body,
            "comment_id": comment_id,
        },
    )


def _review_comment(payload: dict[str, Any], own_login: str | None) -> Intent:
    fields = _comment_fields(payload, own_login)
    if fields is None:
        return None
    comment, body, comment_id = fields
    number = _number((payload.get("pull_request") or {}).get("number"))
    if number is None:
        return None
    return (
        "review_comment",
        {
            "pr_number": number,
            "author": (comment.get("user") or {}).get("login", "unknown"),
            "comment": body,
            "comment_id": comment_id,
        },
    )


def _workflow_run(payload: dict[str, Any]) -> Intent:
    """CI finished. Only failures are interesting; successes are picked up by the polling pass.

    ``workflow_run`` is preferred over ``check_suite`` because it names the workflow that failed.
    The conclusion set is shared with the check-run reader so the two cannot disagree.
    """
    if payload.get("action") != "completed":
        return None
    run = payload.get("workflow_run") or {}
    if run.get("conclusion") not in FAILING_CONCLUSIONS:
        return None
    pulls = run.get("pull_requests") or []
    if not pulls:
        return None
    number = _number(pulls[0].get("number"))
    if number is None:
        return None
    return (
        "ci_failed",
        {"pr_number": number, "sha": _sha(run.get("head_sha"))},
    )


def _pull_request(payload: dict[str, Any]) -> Intent:
    if payload.get("action") != "closed":
        return None
    number = _number((payload.get("pull_request") or {}).get("number"))
    if number is None:
        return None
    return (
        "pr_closed",
        {"pr_number": number, "merged": bool((payload["pull_request"]).get("merged"))},
    )


#: Every kind :func:`to_intent` can emit. The orchestrator asserts it handles all of them, so a
#: renamed handler arm is a test failure rather than a silently dead webhook path.
EMITTED_KINDS = frozenset(
    {"issue_labeled", "issue_comment", "review_comment", "ci_failed", "pr_closed"}
)
