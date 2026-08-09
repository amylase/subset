"""The API clients, against a mock transport.

These were entirely untested, and mutation testing showed why that mattered: every mutation
survived, including `max_attempts=1`, swallowing 4xx instead of raising, reporting a failing check
as success, and dropping the session tags. All of those are silent — nothing crashes, the numbers
just quietly become wrong.
"""

from __future__ import annotations

import json as _json

import httpx
import pytest

from app.clients.devin import DevinClient, last_devin_message
from app.clients.github import GitHubClient
from app.clients.http import ApiError, request_with_retry


@pytest.fixture
def sleeps(monkeypatch):
    """Record backoff delays instead of waiting for them."""
    recorded: list[float] = []

    async def fake_sleep(delay: float) -> None:
        recorded.append(delay)

    monkeypatch.setattr("app.clients.http.asyncio.sleep", fake_sleep)
    return recorded


# --- retry and backoff ------------------------------------------------------


async def test_a_500_is_retried_then_succeeds(sleeps):
    responses = [httpx.Response(500), httpx.Response(500), httpx.Response(200, json={"ok": True})]
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses[len(seen) - 1]

    retries = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await request_with_retry(
            client, "GET", "https://x/y", on_retry=lambda: retries.append(1)
        )
    assert response.json() == {"ok": True}
    assert len(seen) == 3
    assert len(retries) == 2


async def test_backoff_grows_between_attempts(sleeps):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(503))
    ) as client:
        with pytest.raises(ApiError):
            await request_with_retry(client, "GET", "https://x/y", max_attempts=4, base_delay=1.0)
    assert len(sleeps) == 3
    assert sleeps == sorted(sleeps), f"delays must not shrink: {sleeps}"
    assert sleeps[-1] >= 4.0


async def test_retry_after_is_honoured_over_computed_backoff(sleeps):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "7"}))
    ) as client:
        with pytest.raises(ApiError):
            await request_with_retry(client, "GET", "https://x/y", max_attempts=2)
    assert sleeps == [7.0], "a server-provided delay beats a guessed one"


async def test_a_404_is_raised_immediately(sleeps):
    """Swallowing 4xx would make every GitHub write silently no-op."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(404, text="nope")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ApiError) as exc:
            await request_with_retry(client, "GET", "https://x/y")
    assert exc.value.status == 404
    assert len(seen) == 1
    assert sleeps == []


async def test_a_post_is_not_repeated_after_an_ambiguous_failure(sleeps):
    """The single most expensive bug the retry layer can have.

    `POST /sessions` costs money. A 502 from an edge, or a read timeout while Devin provisions, says
    nothing about whether the origin already created the session — retrying it made up to five real
    sessions for one attempt, of which the orchestrator recorded only the last. The rest were never
    polled, never closed, and invisible to the ACU budget.
    """
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(502, text="bad gateway")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ApiError):
            await request_with_retry(client, "POST", "https://x/sessions")
    assert len(seen) == 1
    assert sleeps == []


async def test_a_post_is_not_repeated_after_a_timeout(sleeps):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise httpx.ReadTimeout("too slow", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.ReadTimeout):
            await request_with_retry(client, "POST", "https://x/sessions")
    assert len(seen) == 1


async def test_a_rate_limited_post_is_repeated(sleeps):
    """429 is the exception: the request was refused before it ran, so repeating it is free."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) < 3:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"session_id": "devin-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await request_with_retry(client, "POST", "https://x/sessions")
    assert response.json()["session_id"] == "devin-1"
    assert len(seen) == 3


async def test_a_get_is_still_repeated_after_a_5xx(sleeps):
    """The gate is on the method, not on retrying in general — polling must survive a blip."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True}) if len(seen) > 2 else httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert (await request_with_retry(client, "GET", "https://x/y")).json() == {"ok": True}
    assert len(seen) == 3


# --- GitHub: the gate for the whole review-fix loop -------------------------


def _github(handler) -> GitHubClient:
    client = GitHubClient("tok", "https://api.github.test", "o/r")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: SLF001
    return client


def _checks(runs, total=None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"total_count": total or len(runs), "check_runs": runs})

    return handler


def run(name="job", status="completed", conclusion="success"):
    return {"name": name, "status": status, "conclusion": conclusion}


async def test_an_in_progress_check_is_not_settled():
    client = _github(_checks([run(), run("slow", status="in_progress", conclusion=None)]))
    assert await client.checks_settled("sha") == (False, "pending")


async def test_zero_check_runs_is_pending_not_success():
    """On a fork checks may never run; reporting success would fabricate green CI."""
    client = _github(_checks([]))
    assert await client.checks_settled("sha") == (False, "pending")


@pytest.mark.parametrize(
    "conclusion",
    ["failure", "timed_out", "cancelled", "action_required"],
)
async def test_every_failing_conclusion_is_reported_as_failure(conclusion):
    client = _github(_checks([run(), run("bad", conclusion=conclusion)]))
    assert await client.checks_settled("sha") == (True, "failure")


@pytest.mark.parametrize("conclusion", ["success", "skipped", "neutral"])
async def test_non_blocking_conclusions_pass(conclusion):
    """Superset skips the large majority of its matrix by design."""
    client = _github(_checks([run("a", conclusion=conclusion), run("b")]))
    assert await client.checks_settled("sha") == (True, "success")


async def test_the_failing_sets_agree():
    """They used to differ: `cancelled` made CI read as failed while naming no check at all,
    so the orchestrator decided CI had failed and then had nothing to hand back — forever."""
    client = _github(_checks([run("cancelled-job", conclusion="cancelled")]))
    settled, conclusion = await client.checks_settled("sha")
    assert (settled, conclusion) == (True, "failure")
    assert await client.failed_check_summary("sha") == ["cancelled-job"]


async def test_check_runs_are_paginated():
    """apache/superset reports ~200 check runs; a failure on page 2 used to be invisible."""
    page_one = [run(f"ok-{i}") for i in range(100)]
    page_two = [run("late-failure", conclusion="failure")]

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", 1))
        runs = page_one if page == 1 else page_two
        return httpx.Response(200, json={"total_count": 101, "check_runs": runs})

    client = _github(handler)
    assert await client.checks_settled("sha") == (True, "failure")
    assert await client.failed_check_summary("sha") == ["late-failure"]


async def test_pull_requests_are_excluded_from_the_issue_resync():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=[{"number": 1}, {"number": 2, "pull_request": {"url": "..."}}]
        )

    client = _github(handler)
    assert [i["number"] for i in await client.list_issues_with_label("devin-fix")] == [1]


# --- Devin: the create-session contract -------------------------------------


def _devin(handler) -> DevinClient:
    client = DevinClient("cog_x", "https://api.devin.test/v3/organizations/org-1")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: SLF001
    return client


async def test_create_session_sends_the_v3_contract():
    """Reviewers cross-check the tags shown in the Devin dashboard against what we claim to send."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.read()
        return httpx.Response(200, json={"session_id": "devin-1", "url": "https://x"})

    client = _devin(handler)
    await client.create_session(
        "do the thing",
        title="t",
        tags=["orchestrator:superset-remediation", "issue:2"],
        repo="amylase/superset",
        max_acu_limit=20,
        playbook_id="playbook-1",
    )
    body = _json.loads(captured["body"])
    assert "/v3/organizations/org-1/sessions" in captured["url"]
    assert body["tags"] == ["orchestrator:superset-remediation", "issue:2"]
    assert body["repos"] == ["amylase/superset"]
    assert body["max_acu_limit"] == 20
    assert body["structured_output_required"] is True
    assert body["bypass_approval"] is True
    assert body["playbook_id"] == "playbook-1"


async def test_playbook_id_is_omitted_when_absent():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read()
        return httpx.Response(200, json={"session_id": "s"})

    client = _devin(handler)
    await client.create_session("p", title="t", tags=[], repo="o/r", max_acu_limit=5)
    assert "playbook_id" not in _json.loads(captured["body"])


async def test_the_v3_base_is_used():
    """v1 usage is a documented regression risk; nothing else guards against it."""
    from app.config import Settings

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        devin_api_key="k",
        devin_org_id="org-9",
        github_token="g",
        webhook_secret="0123456789abcdef0123",
    )
    assert settings.org_base == "https://api.devin.ai/v3/organizations/org-9"


# --- Devin: reading the conversation ----------------------------------------


def message(source: str, text: str, at: int) -> dict:
    return {"event_id": f"evt-{at}", "source": source, "message": text, "created_at": at}


def test_last_devin_message_reads_the_real_response_shape():
    """Rows are under `items` and authorship is `source`.

    Guessing `messages`/`type` matched nothing, so the function returned "" on every real call and
    escalation comments never carried the question.
    """
    payload = {
        "items": [
            message("devin", "starting", 1),
            message("user", "go ahead", 2),
            message("devin", "which migration path?", 3),
        ],
        "has_next_page": False,
    }
    assert last_devin_message(payload) == "which migration path?"


def test_the_orchestrators_own_message_is_not_quoted_back():
    payload = {"items": [message("devin", "a question", 1), message("user", "CI failed", 2)]}
    assert last_devin_message(payload) == "a question"


def test_ordering_uses_created_at_not_position():
    payload = {"items": [message("devin", "newer", 9), message("devin", "older", 1)]}
    assert last_devin_message(payload) == "newer"


@pytest.mark.parametrize("payload", [{}, {"items": []}, [], None, {"items": [1, 2]}])
def test_unreadable_message_payloads_degrade_to_empty(payload):
    assert last_devin_message(payload) == ""


# --- the fakes must not drift from the real clients --------------------------


def test_the_fakes_mirror_the_real_client_signatures():
    """A drifted signature makes the whole integration suite a fiction.

    Two mutations proved it: making the real `send_message` keyword-only, and renaming the real
    `comment`, both left 216 tests green while production would raise on every message and every
    comment. The parity was correct by hand and guarded by nothing.
    """
    import inspect

    from tests.fakes import FakeDevin, FakeGitHub

    for real, fake in ((DevinClient, FakeDevin), (GitHubClient, FakeGitHub)):
        shared = {
            name
            for name in vars(fake)
            if not name.startswith("_") and inspect.iscoroutinefunction(getattr(fake, name))
        } & {name for name in vars(real) if not name.startswith("_")}
        assert shared, f"{fake.__name__} shares no methods with {real.__name__}"
        for name in sorted(shared):
            real_sig = inspect.signature(getattr(real, name)).parameters
            fake_sig = inspect.signature(getattr(fake, name)).parameters
            assert fake_sig == real_sig, f"{fake.__name__}.{name} has drifted from {real.__name__}"
            assert inspect.iscoroutinefunction(getattr(real, name)), name


def _client_call_sites() -> dict[str, set[str]]:
    """Every ``self.devin.X(...)`` / ``self.github.X(...)`` the application actually makes.

    Read from the source rather than kept by hand: a hand-written list asserts what someone
    remembered, and the thing being guarded against is precisely a call site nobody remembered.
    """
    import ast
    import pathlib

    import app

    found: dict[str, set[str]] = {"devin": set(), "github": set()}
    for path in pathlib.Path(app.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            fn = node.func if isinstance(node, ast.Call) else None
            if (
                isinstance(fn, ast.Attribute)
                and isinstance(fn.value, ast.Attribute)
                and fn.value.attr in found
            ):
                found[fn.value.attr].add(fn.attr)
    return found


def test_every_client_method_the_application_calls_exists_on_the_real_client_and_its_fake():
    """Both halves, from one list, because each half fails differently.

    Missing on the fake, the integration suite proves nothing about that path. Missing on the real
    client, production raises on a call the suite says works — renaming `GitHubClient.comment` used
    to leave 238 tests green while every comment the system writes would have crashed.
    """
    import inspect

    from tests.fakes import FakeDevin, FakeGitHub

    calls = _client_call_sites()
    assert calls["devin"] and calls["github"], "the source scan found nothing; it has drifted"

    for attr, real, fake in (
        ("devin", DevinClient, FakeDevin),
        ("github", GitHubClient, FakeGitHub),
    ):
        for name in sorted(calls[attr]):
            assert hasattr(real, name), f"{real.__name__} has no {name}(), but the app calls it"
            assert hasattr(fake, name), f"{fake.__name__} is missing {name}()"
            assert (
                inspect.signature(getattr(fake, name)).parameters
                == inspect.signature(getattr(real, name)).parameters
            ), f"{fake.__name__}.{name} has drifted from {real.__name__}"
