"""The application seams.

Two things checked here that unit tests structurally cannot: that the admin write endpoints are
actually guarded, and that the whole read path works against rows a real ``Repo`` produced. The
latter catches a schema rename, which would otherwise break the dashboard in production while every
unit test stayed green.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.metrics import compute
from app.core.state import IssueStatus

ENV = {
    "DEVIN_API_KEY": "cog_test",
    "DEVIN_ORG_ID": "org-test",
    "GITHUB_TOKEN": "gh_test",
    "WEBHOOK_SECRET": "0123456789abcdef0123",
    "DB_PATH": "",  # filled per test
    "SELF_LOGIN": "orchestrator-bot",
    # The loop ticks once immediately on startup. Push the slow passes far out so no test reaches
    # the network with placeholder credentials.
    "SESSION_POLL_INTERVAL": "3600",
    "RESYNC_INTERVAL": "864000",
    "PR_POLL_INTERVAL": "864000",
}


def _build(tmp_path, monkeypatch, **overrides):
    from app.config import get_settings

    for name, value in {**ENV, "DB_PATH": str(tmp_path / "app.db"), **overrides}.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    from app.main import app

    return app, get_settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    app, get_settings = _build(tmp_path, monkeypatch, ADMIN_TOKEN="admin-secret")
    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


def test_the_read_endpoints_respond(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/metrics").status_code == 200
    assert client.get("/api/issues").status_code == 200
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "Resolution rate" in dashboard.text


def test_the_identity_is_resolved_at_startup(client):
    assert client.app.state.own_login == "orchestrator-bot"


def test_the_identity_falls_back_to_asking_github(tmp_path, monkeypatch):
    """`SELF_LOGIN` is optional, so this is the default deployment path and it had no test."""
    from app.clients.github import GitHubClient

    async def whoami(self):
        return "bot-x"

    monkeypatch.setattr(GitHubClient, "whoami", whoami)
    # Empty rather than unset: `Settings` also reads `.env`, so deleting the variable would pick up
    # whatever the developer's own file happens to say.
    app, get_settings = _build(tmp_path, monkeypatch, SELF_LOGIN="")
    with TestClient(app) as test_client:
        assert test_client.app.state.own_login == "bot-x"
    get_settings.cache_clear()


def test_an_unresolvable_identity_starts_but_forwards_nothing(tmp_path, monkeypatch):
    """Failing closed is the right call — our own escalation comment is indistinguishable from a
    maintainer's, and forwarding it back fed the loop its own output. But it must *start*: a
    process that refuses to boot on a GitHub hiccup takes the webhook receiver down with it."""
    import hashlib
    import hmac
    import json

    from app.clients.github import GitHubClient

    async def whoami(self):
        raise RuntimeError("github is having a moment")

    monkeypatch.setattr(GitHubClient, "whoami", whoami)
    # Empty rather than unset: `Settings` also reads `.env`, so deleting the variable would pick up
    # whatever the developer's own file happens to say.
    app, get_settings = _build(tmp_path, monkeypatch, SELF_LOGIN="")
    with TestClient(app) as test_client:
        assert not test_client.app.state.own_login
        repo = test_client.app.state.repo

        def send(event: str, payload: dict, delivery: str):
            body = json.dumps(payload).encode()
            signature = hmac.new(ENV["WEBHOOK_SECRET"].encode(), body, hashlib.sha256).hexdigest()
            return test_client.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Event": event,
                    "X-GitHub-Delivery": delivery,
                    "X-Hub-Signature-256": f"sha256={signature}",
                    "Content-Type": "application/json",
                },
            )

        assert (
            send(
                "issue_comment",
                {
                    "action": "created",
                    "issue": {"number": 2},
                    "comment": {
                        "id": 1,
                        "body": "go ahead",
                        "user": {"login": "someone", "type": "User"},
                        "author_association": "OWNER",
                    },
                },
                "d-1",
            ).status_code
            == 202
        )
        assert repo.pending_inbox() == [], "nothing may be forwarded while identity is unknown"

        assert (
            send(
                "issues",
                {
                    "action": "labeled",
                    "issue": {"number": 2, "state": "open", "labels": [{"name": "devin-fix"}]},
                    "label": {"name": "devin-fix"},
                },
                "d-2",
            ).status_code
            == 202
        )
        assert [i["kind"] for i in repo.pending_inbox()] == ["issue_labeled"], (
            "labelling does not depend on knowing our own identity"
        )
    get_settings.cache_clear()


def test_the_shipped_bounds_are_the_ones_the_readme_documents(tmp_path, monkeypatch):
    """The test fixtures override every one of these, so a change to a shipped default is
    invisible: raising `max_ci_feedback_rounds` to 99 in production passed 238 tests."""
    from app.config import Settings

    settings = Settings(
        _env_file=None,
        devin_api_key="x",
        devin_org_id="x",
        github_token="x",
        webhook_secret="0123456789abcdef",
    )
    assert settings.max_concurrent_sessions == 2
    assert settings.max_acu_per_session == 20
    assert settings.global_acu_budget == 250.0
    assert settings.max_nudges == 2
    assert settings.max_ci_feedback_rounds == 3
    assert settings.message_grace_seconds == 120.0
    assert settings.max_session_age_hours == 12.0
    assert settings.pr_stale_hours == 24.0


@pytest.mark.parametrize(
    ("seconds", "rendered"),
    [
        (None, "—"),
        (0.0, "0s"),
        (45.0, "45s"),
        (600.0, "10m"),
        (7200.0, "2.0h"),
        (259200.0, "3.0d"),
    ],
)
def test_durations_render_in_the_unit_a_reader_expects(seconds, rendered):
    """This is the MTTR column a VP reads. `None` must be an em dash — rendering it as `0s` claims
    a measurement that was never taken, which is the one error worth catching here."""
    from app.main import _fmt_duration

    assert _fmt_duration(seconds) == rendered


@pytest.mark.parametrize("headers", [{}, {"X-Admin-Token": "guess"}])
def test_admin_endpoints_reject_a_bad_token(client, headers):
    assert client.post("/api/admin/issues/5", headers=headers).status_code == 401


def test_a_non_ascii_token_is_rejected_not_a_server_error(client):
    """Starlette decodes headers as latin-1 and `compare_digest` raises on non-ASCII `str`, so an
    unencoded comparison turned this into a 500 — an oracle for whether the API is enabled."""
    response = client.post("/api/admin/issues/5", headers={"X-Admin-Token": "ÿ".encode("latin-1")})
    assert response.status_code == 401


def test_admin_endpoints_accept_the_configured_token(client):
    response = client.post("/api/admin/issues/5", headers={"X-Admin-Token": "admin-secret"})
    assert response.status_code == 200
    assert response.json() == {"status": "queued", "issue": 5}


def test_admin_reconcile_runs_a_tick(client):
    before = client.app.state.orchestrator._tick
    response = client.post("/api/admin/reconcile", headers={"X-Admin-Token": "admin-secret"})
    assert response.status_code == 200
    assert client.app.state.orchestrator._tick > before


def test_the_admin_api_is_absent_unless_configured(tmp_path, monkeypatch):
    """Unset means off, and the 404 does not reveal whether a token was supplied."""
    app, get_settings = _build(tmp_path, monkeypatch, ADMIN_TOKEN=None)
    with TestClient(app) as test_client:
        assert test_client.post("/api/admin/reconcile").status_code == 404
        assert (
            test_client.post(
                "/api/admin/reconcile", headers={"X-Admin-Token": "anything"}
            ).status_code
            == 404
        )
    get_settings.cache_clear()


def test_an_empty_webhook_secret_fails_at_startup(tmp_path, monkeypatch):
    """An empty secret still produces a valid HMAC, so the whole trust model would collapse
    silently — including the `author_association` gate, which becomes forgeable."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _build(tmp_path, monkeypatch, WEBHOOK_SECRET="")[1]()


def test_the_dashboard_renders_real_rows(client):
    """Rendered empty, the route could return an empty table and still pass."""
    repo = client.app.state.repo
    repo.register_issue(2, "a stubborn bug", "class:security")
    repo.create_session("devin-1", 2, url=None, tags=["issue:2"], attempt=1)
    repo.record_poll("devin-1", status="running", status_detail="finished", acus=6.0, produced=True)
    labeled_at = repo.issue(2)["first_labeled_at"]
    repo.upsert_pr(10, issue_number=2, session_id="devin-1", url="u", opened_at=labeled_at + 100)
    repo.update_pr(10, merged_at=labeled_at + 500)
    repo.register_issue(3, "waiting its turn", None)

    repo.register_issue(4, "stuck on a question", None)
    repo.flag_for_human(4, "blocked_on_question")

    page = client.get("/").text
    assert "a stubborn bug" in page
    assert "waiting its turn" in page
    assert "s-merged" in page
    assert "s-queued" in page
    assert "6.00" in page
    # The escalation block renders only when something is wrong, so a typo in it breaks the page at
    # exactly the moment a human is looking for the reason.
    assert "stuck on a question" in page
    assert "blocked on question" in page


def test_metrics_read_the_columns_the_repo_actually_writes(client):
    repo = client.app.state.repo
    repo.register_issue(2, "a bug", "class:logic-bug")
    repo.create_session("devin-1", 2, url="u", tags=[], attempt=1)
    repo.record_poll(
        "devin-1",
        status="running",
        status_detail="finished",
        acus=6.0,
        structured_output={"outcome": "fixed"},
        produced=True,
    )
    labeled_at = repo.issue(2)["first_labeled_at"]
    repo.upsert_pr(10, issue_number=2, session_id="devin-1", url="u", opened_at=labeled_at + 100)
    repo.update_pr(
        10, ci_settled_at=labeled_at + 200, ci_conclusion="success", merged_at=labeled_at + 500
    )

    view = client.app.state.orchestrator.issue_view()
    assert view[0]["status"] is IssueStatus.MERGED

    m = compute(
        view=view,
        sessions=repo.sessions(),
        pull_requests=repo.pull_requests(),
        interventions=repo.interventions(),
        counters=repo.counters(),
        acu_unit_cost_usd=2.0,
        manual_effort_hours_per_issue=4.0,
        engineer_hourly_usd=100.0,
    )
    assert m.issues_resolved == 1
    assert m.resolution_rate == 1.0
    assert m.acus_total == 6.0
    assert m.cost_per_resolution_usd == 12.0
    assert m.durations.agent == 100.0
    assert m.durations.ci == 100.0
    assert m.durations.human_review == 300.0
    assert m.autonomy_rate == 1.0
