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

    async def latest_devin_message(self, session_id: str) -> str:
        """The newest Devin turn, following pagination to the end.

        Messages are returned oldest-first with no reverse option, so reading one page returns the
        *start* of the conversation. On a session with more than a page of turns that meant the
        escalation comment quoted an opening remark instead of the question that actually blocked
        it — on the one path where getting it right matters.
        """
        cursor: str | None = None
        latest = ""
        for _ in range(20):
            params: dict[str, Any] = {"first": 200}
            if cursor:
                params["after"] = cursor
            page = await self._call("GET", f"/sessions/{session_id}/messages", params=params)
            found = last_devin_message(page)
            if found:
                latest = found
            if not isinstance(page, dict) or not page.get("has_next_page"):
                break
            cursor = page.get("end_cursor")
            if not cursor:
                break
        return latest

    async def insights(
        self, *, tags: list[str] | None = None, first: int = 100, after: str | None = None
    ) -> Any:
        """Session analytics: ACUs, message counts, size classification.

        The documentation renders these parameters as nested under a ``qs`` object, which is an
        OpenAPI artefact — the validator reads them at the top level. Confirmed against the live
        endpoint: ``?first=201`` returns 422 naming ``query.first``, so ``first`` maxes at 200 and
        flat keys are correct. httpx encodes a list as repeated keys, which the endpoint accepts.
        """
        params: dict[str, Any] = {"first": min(first, 200)}
        if tags:
            params["tags"] = tags
        if after:
            params["after"] = after
        return await self._call("GET", "/sessions/insights", params=params)

    # --- playbooks and schedules ------------------------------------------

    async def list_playbooks(self) -> Any:
        return await self._call("GET", "/playbooks")

    async def create_playbook(self, title: str, body: str) -> Any:
        """Create a playbook.

        The field is ``title``, not ``name`` — the API rejects ``name`` with a 422. Confirmed
        against the live endpoint rather than inferred from the docs.
        """
        return await self._call("POST", "/playbooks", json={"title": title, "body": body})

    async def list_schedules(self) -> Any:
        return await self._call("GET", "/schedules")

    async def create_schedule(self, name: str, prompt: str, frequency: str) -> Any:
        """Create a recurring scheduled session.

        The fields are ``name``, ``prompt`` and ``frequency``. Confirmed against the live endpoint:
        omitting ``name`` returns ``422 name: Field required``, and ``name`` + ``prompt`` alone
        returns ``422 frequency is required for recurring schedules``. An earlier version sent
        ``title``/``cron_schedule``/``timezone`` from a misreading and would have failed every time.
        """
        return await self._call(
            "POST", "/schedules", json={"name": name, "prompt": prompt, "frequency": frequency}
        )


def collection_items(response: Any) -> list[dict[str, Any]]:
    """Rows out of a v3 list response.

    List endpoints return ``{"items": [...], "end_cursor": ..., "has_next_page": ...}``. Tolerant of
    a bare list too, so a shape change degrades to empty rather than raising.
    """
    if isinstance(response, dict):
        for key in ("items", "playbooks", "schedules", "sessions", "messages"):
            value = response.get(key)
            if isinstance(value, list):
                return [v for v in value if isinstance(v, dict)]
        return []
    if isinstance(response, list):
        return [v for v in response if isinstance(v, dict)]
    return []


def last_devin_message(messages: Any) -> str:
    """Extract the most recent Devin-authored message text.

    Used to find out *what* a blocked session asked, so the escalation comment on the issue carries
    the question rather than only a link.

    The real response is ``{"items": [{"event_id", "source", "message", "created_at"}], ...}``:
    rows sit under ``items`` (not ``messages``) and authorship is ``source`` with values ``devin``
    and ``user`` (there is no ``type`` field). An earlier version guessed both names, so it matched
    nothing and returned the empty string on every call against the live API — the "tolerant
    degradation" was the only path that ever ran.

    Messages are ordered oldest-first, so the newest Devin turn is found from the end.
    ``created_at``
    is used to order defensively rather than trusting position.
    """
    items = collection_items(messages)
    if not items:
        return ""
    ordered = sorted(items, key=lambda item: item.get("created_at") or 0)
    for item in reversed(ordered):
        if item.get("source") != "devin":
            continue
        value = item.get("message")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
