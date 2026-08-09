"""Translating GitHub events into orchestrator intent.

A pure function, so the whole event-routing surface is testable without a server, a database, or a
network. It returns ``(kind, payload)`` for events worth acting on and ``None`` for everything else.

Filtering here is what stops the system double-firing. ``issues`` alone is not enough — the same
event type carries ``edited``, ``unlabeled`` and ``reopened``, none of which should start work.
"""

from __future__ import annotations

from typing import Any

from app.clients.github import FAILING_CONCLUSIONS

Intent = tuple[str, dict[str, Any]] | None

#: Who may put text in front of Devin.
#:
#: Anything forwarded to a session reaches an agent holding a checked-out working tree and push
#: rights on the repository, so the forwarding paths are an instruction channel, not a comment feed.
#: The fork is public: without this gate any account could comment on an open remediation pull
#: request and have that text delivered to the agent. ``author_association`` is already present in
#: every comment payload.
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

#: Untrusted text is truncated before it is forwarded. A long comment is not a useful instruction
#: and a very long one is a way to push the real task out of the agent's attention.
MAX_FORWARDED_CHARS = 4000


def _is_trusted(comment: dict[str, Any]) -> bool:
    return comment.get("author_association") in TRUSTED_ASSOCIATIONS


def to_intent(event: str, payload: dict[str, Any], *, trigger_label: str) -> Intent:
    match event:
        case "issues":
            return _issues(payload, trigger_label)
        case "issue_comment":
            return _issue_comment(payload)
        case "workflow_run":
            return _workflow_run(payload)
        case "pull_request_review_comment":
            return _review_comment(payload)
        case "pull_request":
            return _pull_request(payload)
        case _:
            return None


def _issues(payload: dict[str, Any], trigger_label: str) -> Intent:
    if payload.get("action") != "labeled":
        return None
    if (payload.get("label") or {}).get("name") != trigger_label:
        return None
    return "issue_labeled", {"number": payload["issue"]["number"]}


def _issue_comment(payload: dict[str, Any]) -> Intent:
    """A human answering a question the orchestrator escalated.

    Bot comments are skipped, otherwise the orchestrator's own write-back would be forwarded to
    Devin as if it were a human reply. Only trusted associations are forwarded — see
    :data:`TRUSTED_ASSOCIATIONS`.
    """
    if payload.get("action") != "created":
        return None
    comment = payload.get("comment") or {}
    if (comment.get("user") or {}).get("type") == "Bot":
        return None
    if not _is_trusted(comment):
        return None
    body = (comment.get("body") or "").strip()
    if not body:
        return None
    return "issue_comment", {
        "issue_number": payload["issue"]["number"],
        "author": (comment.get("user") or {}).get("login", "unknown"),
        "comment": body[:MAX_FORWARDED_CHARS],
    }


def _workflow_run(payload: dict[str, Any]) -> Intent:
    """CI finished. Only failures are interesting — successes are picked up by the polling pass.

    ``workflow_run`` is preferred over ``check_suite`` because it names the workflow that failed,
    which makes for a far more useful message to hand back to Devin.
    """
    if payload.get("action") != "completed":
        return None
    run = payload.get("workflow_run") or {}
    # Same set the check-run reader uses. `startup_failure` matters in particular: it is what a
    # malformed workflow file produces, and it used to be read as a pass.
    if run.get("conclusion") not in FAILING_CONCLUSIONS:
        return None
    pulls = run.get("pull_requests") or []
    if not pulls:
        return None
    return "ci_failed", {
        "pr_number": pulls[0]["number"],
        "sha": run.get("head_sha"),
        "workflow": run.get("name"),
    }


def _review_comment(payload: dict[str, Any]) -> Intent:
    """Reviewer feedback on a remediation pull request.

    Same trust gate as issue comments. This path used to accept any non-bot commenter, which on a
    public fork meant anyone could send instructions to an agent with push rights.
    """
    if payload.get("action") != "created":
        return None
    comment = payload.get("comment") or {}
    if (comment.get("user") or {}).get("type") == "Bot":
        return None
    if not _is_trusted(comment):
        return None
    body = (comment.get("body") or "").strip()
    if not body:
        return None
    return "review_comment", {
        "pr_number": payload["pull_request"]["number"],
        "reviewer": (comment.get("user") or {}).get("login", "unknown"),
        "comment": body[:MAX_FORWARDED_CHARS],
    }


def _pull_request(payload: dict[str, Any]) -> Intent:
    """A closed pull request settles the outcome; the PR pass confirms merge state itself."""
    if payload.get("action") != "closed":
        return None
    return "pr_closed", {
        "pr_number": payload["pull_request"]["number"],
        "merged": bool(payload["pull_request"].get("merged")),
    }
