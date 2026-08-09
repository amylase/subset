"""The application seams.

Two things are checked here that unit tests structurally cannot: that the admin write endpoints are
actually guarded, and that the metric functions work against rows a real ``Repo`` produces rather
than against hand-built dictionaries. The latter is what catches a schema rename — every metric test
would stay green while the dashboard broke in production.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.metrics import compute
from app.db.repo import Repo


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVIN_API_KEY", "cog_test")
    monkeypatch.setenv("DEVIN_ORG_ID", "org-test")
    monkeypatch.setenv("GITHUB_TOKEN", "gh_test")
    monkeypatch.setenv("WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("SESSION_POLL_INTERVAL", "3600")
    # The loop ticks once immediately on startup. Push the resync and analytics passes far out so
    # this test never reaches the network with placeholder credentials.
    monkeypatch.setenv("RESYNC_INTERVAL", "864000")

    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


def test_health_and_read_endpoints_respond(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/metrics").status_code == 200
    assert client.get("/api/issues").status_code == 200
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "Resolution rate" in dashboard.text


def test_admin_endpoints_reject_a_wrong_token(client):
    response = client.post("/api/admin/issues/5", headers={"X-Admin-Token": "guess"})
    assert response.status_code == 401


def test_admin_endpoints_reject_a_missing_token(client):
    assert client.post("/api/admin/issues/5").status_code == 401


def test_admin_endpoints_accept_the_configured_token(client):
    response = client.post("/api/admin/issues/5", headers={"X-Admin-Token": "admin-secret"})
    assert response.status_code == 200
    assert response.json() == {"status": "queued", "issue": 5}


def test_the_admin_api_is_absent_unless_configured(tmp_path, monkeypatch):
    """Unset means off, and the 404 does not reveal whether a token was supplied."""
    for name, value in {
        "DEVIN_API_KEY": "k",
        "DEVIN_ORG_ID": "org-x",
        "GITHUB_TOKEN": "g",
        "WEBHOOK_SECRET": "s",
        "DB_PATH": str(tmp_path / "b.db"),
        "SESSION_POLL_INTERVAL": "3600",
        "RESYNC_INTERVAL": "864000",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)

    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import app

    with TestClient(app) as test_client:
        assert test_client.post("/api/admin/reconcile").status_code == 404
        assert (
            test_client.post(
                "/api/admin/reconcile", headers={"X-Admin-Token": "anything"}
            ).status_code
            == 404
        )
    get_settings.cache_clear()


def test_metrics_read_the_columns_the_repo_actually_writes(tmp_path):
    """A column rename would break the dashboard while every metrics unit test stayed green."""
    repo = Repo(str(tmp_path / "seam.db"))
    repo.upsert_issue(2, "a bug", "class:logic-bug", labeled_at=0.0)
    repo.create_session("devin-1", 2, "https://app.devin.ai/sessions/devin-1", ["issue:2"])
    repo.update_session(
        "devin-1",
        status="running",
        status_detail="finished",
        acus=6.0,
        structured_output={"outcome": "fixed"},
        finished=True,
    )
    repo.upsert_pr(
        10,
        issue_number=2,
        session_id="devin-1",
        url="https://github.com/o/r/pull/10",
        opened_at=100.0,
        state="open",
    )
    repo.update_pr(10, ci_settled_at=200.0, ci_conclusion="success", merged_at=500.0)

    m = compute(
        issues=repo.issues(),
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
