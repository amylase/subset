"""Recording doubles for the two API clients.

Deliberately dumb: they script responses and record calls, and contain no logic of their own. The
orchestrator is what is under test, so anything clever here would be testing the test.

Every fake method mirrors the real client's signature. If a signature drifts, these break loudly
rather than passing against an interface that no longer exists.
"""

from __future__ import annotations

from typing import Any


class FakeDevin:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.messages: list[tuple[str, str]] = []
        self.message_log: dict[str, Any] = {"items": []}
        #: session_id -> list of responses, consumed one per poll; the last one repeats.
        self.states: dict[str, list[dict[str, Any]]] = {}
        self.get_calls: list[str] = []
        self.create_error: Exception | None = None
        #: Rows the analytics endpoint returns. Includes foreign sessions in some tests, because
        #: the endpoint ignores unknown filters rather than rejecting them.
        self.insight_rows: list[dict[str, Any]] = []
        self.insight_calls: list[Any] = []
        self._next = 1

    # -- scripting ----------------------------------------------------------

    def script(self, session_id: str, *states: dict[str, Any]) -> None:
        self.states[session_id] = list(states)

    @staticmethod
    def state(
        status: str = "running",
        detail: str | None = "working",
        *,
        acus: float = 0.0,
        structured: Any = None,
        pulls: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "status_detail": detail,
            "acus_consumed": acus,
            "structured_output": structured,
            "pull_requests": pulls or [],
        }

    # -- the client interface ----------------------------------------------

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
        if self.create_error is not None:
            raise self.create_error
        session_id = f"devin-{self._next}"
        self._next += 1
        self.created.append(
            {
                "session_id": session_id,
                "prompt": prompt,
                "title": title,
                "tags": tags,
                "repo": repo,
                "max_acu_limit": max_acu_limit,
                "playbook_id": playbook_id,
            }
        )
        self.states.setdefault(session_id, [self.state()])
        return {"session_id": session_id, "url": f"https://app.devin.ai/sessions/{session_id}"}

    async def get_session(self, session_id: str) -> dict[str, Any]:
        self.get_calls.append(session_id)
        states = self.states.get(session_id) or [self.state()]
        return states.pop(0) if len(states) > 1 else states[0]

    async def send_message(self, session_id: str, message: str) -> None:
        self.messages.append((session_id, message))

    async def list_messages(self, session_id: str, *, first: int = 50) -> Any:
        return self.message_log

    async def insights(self, *, tags: list[str] | None = None, first: int = 100) -> Any:
        self.insight_calls.append(tags)
        return {"items": self.insight_rows}


class FakeGitHub:
    def __init__(self) -> None:
        self.issues: dict[int, dict[str, Any]] = {}
        self.pulls: dict[int, dict[str, Any]] = {}
        self.comments: list[tuple[int, str]] = []
        self.labels_added: list[tuple[int, str]] = []
        self.labels_removed: list[tuple[int, str]] = []
        self.checks: dict[str, tuple[bool, str]] = {}
        self.failed_checks: dict[str, list[str]] = {}
        self.labelled_issues: list[dict[str, Any]] = []
        self.comment_error: Exception | None = None
        self.get_issue_error: Exception | None = None

    # -- scripting ----------------------------------------------------------

    def add_issue(self, number: int, *, labels: list[str], title: str = "t", state: str = "open"):
        self.issues[number] = {
            "number": number,
            "title": title,
            "state": state,
            "labels": [{"name": name} for name in labels],
        }

    def add_pull(self, number: int, *, sha: str = "sha1", merged: bool = False, state="open"):
        self.pulls[number] = {
            "number": number,
            "state": "closed" if merged else state,
            "merged_at": "2026-01-01T00:00:00Z" if merged else None,
            "head": {"sha": sha},
        }

    # -- the client interface ----------------------------------------------

    async def get_issue(self, number: int) -> dict[str, Any]:
        if self.get_issue_error is not None:
            raise self.get_issue_error
        return self.issues[number]

    async def list_issues_with_label(self, label: str) -> list[dict[str, Any]]:
        return self.labelled_issues

    async def comment(self, number: int, body: str) -> None:
        if self.comment_error is not None:
            raise self.comment_error
        self.comments.append((number, body))

    async def add_label(self, number: int, label: str) -> None:
        self.labels_added.append((number, label))

    async def remove_label(self, number: int, label: str) -> None:
        self.labels_removed.append((number, label))

    async def get_pull(self, number: int) -> dict[str, Any]:
        return self.pulls[number]

    async def checks_settled(self, sha: str) -> tuple[bool, str]:
        return self.checks.get(sha, (False, "pending"))

    async def failed_check_summary(self, sha: str, *, limit: int = 8) -> list[str]:
        return self.failed_checks.get(sha, [])[:limit]
