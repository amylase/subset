from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from app.db.repo import Repo
from app.webhooks.router import router as webhook_router

SECRET = "test-webhook-secret"
REPO_FULL_NAME = "amylase/superset"


@pytest.fixture
def repo(tmp_path) -> Repo:
    return Repo(str(tmp_path / "test.db"))


@pytest.fixture
def settings() -> SimpleNamespace:
    return SimpleNamespace(
        webhook_secret=SECRET,
        github_repo=REPO_FULL_NAME,
        trigger_label="devin-fix",
        escalation_label="needs-human",
    )


@pytest.fixture
def webhook_app(repo: Repo, settings: SimpleNamespace) -> FastAPI:
    """The receiver mounted in isolation.

    Built without the real lifespan on purpose: the point of the receiver being side-effect free is
    that it can be exercised with no credentials, no network and no background loop.
    """
    app = FastAPI()
    app.include_router(webhook_router)
    app.state.settings = settings
    app.state.repo = repo
    return app
