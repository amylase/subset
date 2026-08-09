"""Translating GitHub events into orchestrator intent.

A pure function, so the whole event-routing surface is testable without a server, a database, or a
network. It returns ``(kind, payload)`` for events worth acting on and ``None`` for everything else.

Filtering here is what stops the system double-firing. ``issues`` alone is not enough — the same
event type carries ``edited``, ``unlabeled`` and ``reopened``, none of which should start work.
"""

from __future__ import annotations

from typing import Any

Intent = tuple[str, dict[str, Any]] | None


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
    Devin as if it were a human reply.
    """
    if payload.get("action") != "created":
        return None
    comment = payload.get("comment") or {}
    if (comment.get("user") or {}).get("type") == "Bot":
        return None
    body = (comment.get("body") or "").strip()
    if not body:
        return None
    return "issue_comment", {
        "issue_number": payload["issue"]["number"],
        "author": (comment.get("user") or {}).get("login", "unknown"),
        "comment": body,
    }


def _workflow_run(payload: dict[str, Any]) -> Intent:
    """CI finished. Only failures are interesting — successes are picked up by the polling pass.

    ``workflow_run`` is preferred over ``check_suite`` because it names the workflow that failed,
    which makes for a far more useful message to hand back to Devin.
    """
    if payload.get("action") != "completed":
        return None
    run = payload.get("workflow_run") or {}
    if run.get("conclusion") not in ("failure", "timed_out"):
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
    if payload.get("action") != "created":
        return None
    comment = payload.get("comment") or {}
    if (comment.get("user") or {}).get("type") == "Bot":
        return None
    return "review_comment", {
        "pr_number": payload["pull_request"]["number"],
        "reviewer": (comment.get("user") or {}).get("login", "unknown"),
        "comment": (comment.get("body") or "").strip(),
    }


def _pull_request(payload: dict[str, Any]) -> Intent:
    """A closed pull request settles the outcome; the PR pass confirms merge state itself."""
    if payload.get("action") != "closed":
        return None
    return "pr_closed", {
        "pr_number": payload["pull_request"]["number"],
        "merged": bool(payload["pull_request"].get("merged")),
    }
