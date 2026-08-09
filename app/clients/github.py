"""GitHub REST client.

The write path matters more than the read path here: writing back to the issue is what closes the
loop that the whole system is judged on.

One read deserves explanation. ``get_issue`` exists so the orchestrator can re-fetch an issue rather
than trusting the webhook payload. A verified signature proves GitHub sent the event; it does not
prove the event is still true. A label added and immediately removed would otherwise spend ACUs on
work nobody wants.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.clients.http import request_with_retry

logger = logging.getLogger(__name__)

#: Check-run conclusions that must not be read as a pass.
#:
#: One set, used by both the settled/failed decision and the summary handed to Devin. They were
#: previously two different sets, which produced a silent dead loop: a ``cancelled`` run made
#: ``checks_settled`` report failure while ``failed_check_summary`` returned nothing, so the
#: orchestrator decided CI had failed and then had nothing to say about it, on every poll, forever.
#:
#: ``skipped`` and ``neutral`` are deliberately absent — GitHub treats both as non-blocking, and on
#: Superset's matrix the large majority of runs on any given commit are ``skipped`` by design.
FAILING_CONCLUSIONS = frozenset(
    {
        "failure",
        "timed_out",
        "cancelled",
        "action_required",
        "stale",
        "startup_failure",
    }
)


class GitHubClient:
    def __init__(self, token: str, api_base: str, repo: str, *, on_retry: Any = None) -> None:
        self._base = f"{api_base.rstrip('/')}/repos/{repo}"
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._on_retry = on_retry
        self._client = httpx.AsyncClient(timeout=30.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await request_with_retry(
            self._client,
            method,
            f"{self._base}{path}",
            headers=self._headers,
            on_retry=self._on_retry,
            **kwargs,
        )
        if not response.content:
            return None
        return response.json()

    # --- issues ------------------------------------------------------------

    async def get_issue(self, number: int) -> dict[str, Any]:
        return await self._call("GET", f"/issues/{number}")

    async def list_issues_with_label(self, label: str) -> list[dict[str, Any]]:
        """Open issues carrying ``label``. Backs the resync pass that recovers lost webhooks."""
        items = await self._call(
            "GET", "/issues", params={"labels": label, "state": "open", "per_page": 100}
        )
        return [i for i in (items or []) if "pull_request" not in i]

    async def comment(self, number: int, body: str) -> Any:
        return await self._call("POST", f"/issues/{number}/comments", json={"body": body})

    async def add_label(self, number: int, label: str) -> Any:
        return await self._call("POST", f"/issues/{number}/labels", json={"labels": [label]})

    async def remove_label(self, number: int, label: str) -> None:
        try:
            await self._call("DELETE", f"/issues/{number}/labels/{label}")
        except Exception as exc:  # label may already be gone; not worth failing a reconcile pass
            logger.debug("could not remove label %s from #%s: %s", label, number, exc)

    # --- pull requests -----------------------------------------------------

    async def get_pull(self, number: int) -> dict[str, Any]:
        return await self._call("GET", f"/pulls/{number}")

    async def pull_files(self, number: int) -> list[dict[str, Any]]:
        return await self._call("GET", f"/pulls/{number}/files", params={"per_page": 100}) or []

    async def check_runs(self, sha: str) -> list[dict[str, Any]]:
        """Every check run for a commit, following pagination.

        Paging is not optional here. `apache/superset` reports ``total_count`` near 200 on master
        while a page holds 100, so reading only the first page hid failures on later pages and
        reported CI green — which would both skip the self-correction loop and record an MTTR
        against a pull request whose CI never passed.
        """
        runs: list[dict[str, Any]] = []
        page = 1
        while True:
            data = await self._call(
                "GET", f"/commits/{sha}/check-runs", params={"per_page": 100, "page": page}
            )
            batch = data.get("check_runs", [])
            runs.extend(batch)
            total = int(data.get("total_count") or 0)
            if not batch or len(runs) >= total or page >= 20:
                return runs
            page += 1

    async def failed_check_summary(self, sha: str, *, limit: int = 8) -> list[str]:
        """Names of the checks that did not pass, for handing back to Devin."""
        runs = await self.check_runs(sha)
        failed = [run["name"] for run in runs if run.get("conclusion") in FAILING_CONCLUSIONS]
        return failed[:limit]

    async def checks_settled(self, sha: str) -> tuple[bool, str]:
        """Whether every check on ``sha`` has finished, and the aggregate conclusion.

        Returns ``(settled, conclusion)`` where conclusion is ``success``, ``failure`` or
        ``pending``. On a fork some checks never run at all, which is why the baseline recorded in
        the README matters when reading this.
        """
        runs = await self.check_runs(sha)
        if not runs:
            # No checks is not success. Fabricating green here would inflate the first-pass rate
            # and merge pull requests nothing ever verified.
            return False, "pending"
        if any(run.get("status") != "completed" for run in runs):
            return False, "pending"
        if any(run.get("conclusion") in FAILING_CONCLUSIONS for run in runs):
            return True, "failure"
        return True, "success"
