"""Turning GitHub events into orchestrator intent, with provenance attached.

A pure function, so the whole routing and trust surface is testable without a server, a database or
a network.

Provenance is assigned here and never re-derived. Everything the orchestrator later forwards to a
Devin session reaches an agent holding a checked-out working tree and push rights on a public
repository, so these paths are an instruction channel rather than a comment feed.

The identity check comes first and matters most. The orchestrator writes to GitHub with a personal
access token, so its own comments arrive as ``type: "User"`` with ``author_association: "OWNER"`` —
v1's bot filter did not match them and the trust gate therefore whitelisted the system's own
escalation comments, which were forwarded to Devin as human answers, which reset the nudge budget,
which produced another escalation. An infinite loop on the first escalation.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from app.clients.github import FAILING_CONCLUSIONS


class Provenance(StrEnum):
    SELF = "self"
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"
    SYSTEM = "system"


#: Associations that imply repository trust. Note this is repository *access*: `COLLABORATOR`
#: includes read and triage collaborators, not only those who can write.
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

#: Untrusted text is truncated before it is forwarded. A very long comment is not a useful
#: instruction, and it is a way to push the real task out of the agent's attention.
MAX_FORWARDED_CHARS = 4000

Intent = tuple[str, dict[str, Any], Provenance] | None


def classify(comment: dict[str, Any], *, own_login: str | None) -> Provenance:
    """Who is speaking. Identity is checked before association."""
    user = comment.get("user") or {}
    login = user.get("login")
    if own_login and login and login.lower() == own_login.lower():
        return Provenance.SELF
    if user.get("type") == "Bot":
        return Provenance.SELF
    if comment.get("author_association") in TRUSTED_ASSOCIATIONS:
        return Provenance.TRUSTED
    return Provenance.UNTRUSTED


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
    return "issue_labeled", {"number": number}, Provenance.SYSTEM


def _comment_fields(
    payload: dict[str, Any], own_login: str | None
) -> tuple[dict[str, Any], str, int, Provenance] | None:
    if payload.get("action") != "created":
        return None
    comment = payload.get("comment") or {}
    provenance = classify(comment, own_login=own_login)
    if provenance is not Provenance.TRUSTED:
        return None
    body = (comment.get("body") or "").strip()
    comment_id = _number(comment.get("id"))
    if not body or comment_id is None:
        return None
    return comment, body[:MAX_FORWARDED_CHARS], comment_id, provenance


def _issue_comment(payload: dict[str, Any], own_login: str | None) -> Intent:
    fields = _comment_fields(payload, own_login)
    if fields is None:
        return None
    comment, body, comment_id, provenance = fields
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
        provenance,
    )


def _review_comment(payload: dict[str, Any], own_login: str | None) -> Intent:
    fields = _comment_fields(payload, own_login)
    if fields is None:
        return None
    comment, body, comment_id, provenance = fields
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
        provenance,
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
        {"pr_number": number, "sha": run.get("head_sha"), "workflow": run.get("name")},
        Provenance.SYSTEM,
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
        Provenance.SYSTEM,
    )


#: Every kind :func:`to_intent` can emit. The orchestrator asserts it handles all of them, so a
#: renamed handler arm is a test failure rather than a silently dead webhook path.
EMITTED_KINDS = frozenset(
    {"issue_labeled", "issue_comment", "review_comment", "ci_failed", "pr_closed"}
)
