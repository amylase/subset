from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from app.config import Settings
from app.core.effects import Effects
from app.core.orchestrator import Orchestrator
from app.db.repo import Repo
from app.webhooks.router import router as webhook_router
from tests.fakes import FakeDevin, FakeGitHub

SECRET = "test-webhook-secret-long-enough"
REPO_FULL_NAME = "amylase/superset"
TRIGGER = "devin-fix"
OWN_LOGIN = "orchestrator-bot"


@pytest.fixture
def repo(tmp_path) -> Repo:
    return Repo(str(tmp_path / "test.db"))


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Real Settings, not a stand-in.

    A `SimpleNamespace` would hide a renamed field from every test that uses it, which is exactly
    the kind of seam that let v1's suite stay green while the app was misconfigured.
    """
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        devin_api_key="cog_test",
        devin_org_id="org-test",
        github_token="gh_test",
        webhook_secret=SECRET,
        github_repo=REPO_FULL_NAME,
        db_path=str(tmp_path / "orc.db"),
        max_concurrent_sessions=2,
        max_acu_per_session=20,
        global_acu_budget=100,
        max_nudges=2,
        max_ci_feedback_rounds=2,
        message_grace_seconds=0.0,
        max_session_age_hours=12,
        devin_playbook_id="playbook-xyz",
        self_login=OWN_LOGIN,
    )


@pytest.fixture
def orc(settings):
    """An orchestrator wired to a real Repo, a real Effects and fake clients."""
    repo = Repo(settings.db_path)
    devin, github = FakeDevin(), FakeGitHub()
    effects = Effects(settings, repo, devin, github)
    return Orchestrator(settings, repo, effects), repo, devin, github


@pytest.fixture
def webhook_app(repo: Repo) -> FastAPI:
    """The receiver mounted in isolation.

    Built without the real lifespan on purpose: the point of the receiver being side-effect free is
    that it can be exercised with no credentials, no network and no background loop.
    """
    app = FastAPI()
    app.include_router(webhook_router)
    app.state.settings = SimpleNamespace(
        webhook_secret=SECRET, github_repo=REPO_FULL_NAME, trigger_label=TRIGGER
    )
    app.state.repo = repo
    app.state.own_login = OWN_LOGIN
    return app
