"""Devin API v3 client.

Only the organization-scoped v3 endpoints are used. The enterprise consumption endpoints
(``/v3/enterprise/consumption/*``) return 403 for an org-scoped service user, which is why cost
reporting is derived from ``insights.acus_consumed`` instead.

Tags set here are the same strings a reviewer sees in the Devin dashboard, so they are built from
real identifiers and never decorated for presentation.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.clients.http import request_with_retry

logger = logging.getLogger(__name__)

#: Schema every remediation session must fill in. Requiring structured output gives the
#: orchestrator a contractual completion signal instead of guessing from free text.
REMEDIATION_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["outcome", "summary"],
    "properties": {
        "outcome": {
            "type": "string",
            "enum": ["fixed", "partially_fixed", "could_not_fix"],
            "description": "Honest assessment of what was achieved.",
        },
        "summary": {
            "type": "string",
            "description": "What the root cause was and how it was fixed.",
        },
        "root_cause": {"type": "string", "description": "The underlying defect, in one paragraph."},
        "pull_request_url": {"type": "string"},
        "tests_added": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Test files or test names added or changed.",
        },
        "assumptions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Decisions taken without confirmation, and why.",
        },
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "follow_up": {
            "type": "string",
            "description": "Anything intentionally left undone, or empty if nothing.",
        },
    },
}


class DevinClient:
    def __init__(self, api_key: str, org_base: str, *, on_retry: Any = None) -> None:
        self._org_base = org_base.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._on_retry = on_retry
        self._client = httpx.AsyncClient(timeout=60.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await request_with_retry(
            self._client,
            method,
            f"{self._org_base}{path}",
            headers=self._headers,
            on_retry=self._on_retry,
            **kwargs,
        )
        if not response.content:
            return None
        return response.json()

    # --- sessions ----------------------------------------------------------

    async def create_session(
        self,
        prompt: str,
        *,
        title: str,
        tags: list[str],
        repo: str,
        max_acu_limit: int,
        playbook_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a remediation session.

        ``resumable`` is left at its default of true, which is what makes the review-fix loop
        possible: the VM survives the session going to sleep, so a later message resumes with the
        working tree and environment intact rather than starting over.

        ``bypass_approval`` removes the ``waiting_for_approval`` stall, which is an interactive
        state with no interactive user on this path.
        """
        payload: dict[str, Any] = {
            "prompt": prompt,
            "title": title,
            "tags": tags,
            "repos": [repo],
            "max_acu_limit": max_acu_limit,
            "bypass_approval": True,
            "structured_output_required": True,
            "structured_output_schema": REMEDIATION_OUTPUT_SCHEMA,
        }
        if playbook_id:
            payload["playbook_id"] = playbook_id
        logger.info("creating Devin session: title=%r tags=%s", title, tags)
        return await self._call("POST", "/sessions", json=payload)

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return await self._call("GET", f"/sessions/{session_id}")

    async def send_message(self, session_id: str, message: str) -> Any:
        """Send a message, which also wakes a sleeping or suspended session."""
        return await self._call(
            "POST", f"/sessions/{session_id}/messages", json={"message": message}
        )

    async def list_messages(self, session_id: str, *, first: int = 50) -> Any:
        return await self._call("GET", f"/sessions/{session_id}/messages", params={"first": first})

    async def insights(self, *, tags: list[str] | None = None, first: int = 100) -> Any:
        """Session analytics: ACUs, message counts, PR states, Devin's own analysis."""
        params: dict[str, Any] = {"first": first}
        if tags:
            params["tags"] = tags
        return await self._call("GET", "/sessions/insights", params=params)

    # --- playbooks and schedules ------------------------------------------

    async def list_playbooks(self) -> Any:
        return await self._call("GET", "/playbooks")

    async def create_playbook(self, name: str, body: str) -> Any:
        return await self._call("POST", "/playbooks", json={"name": name, "body": body})

    async def list_schedules(self) -> Any:
        return await self._call("GET", "/schedules")

    async def create_schedule(
        self, prompt: str, cron: str, *, timezone: str = "UTC", title: str | None = None
    ) -> Any:
        payload: dict[str, Any] = {
            "prompt": prompt,
            "cron_schedule": cron,
            "timezone": timezone,
        }
        if title:
            payload["title"] = title
        return await self._call("POST", "/schedules", json=payload)


def last_devin_message(messages: Any) -> str:
    """Extract the most recent Devin-authored message text.

    Used to find out *what* a blocked session asked, so the escalation comment on the issue carries
    the question rather than just a link. Tolerant of shape changes: the message list format is not
    fully specified in the docs, so anything unrecognised degrades to an empty string.
    """
    items = messages.get("messages") if isinstance(messages, dict) else messages
    if not isinstance(items, list):
        return ""
    for item in reversed(items):
        if not isinstance(item, dict):
            continue
        if item.get("type") in ("user_message", "user"):
            continue
        for key in ("message", "text", "content"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""
