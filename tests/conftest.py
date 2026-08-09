from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from app.config import Settings
from app.core import orchestrator as orchestrator_module
from app.core.effects import Effects
from app.core.orchestrator import Orchestrator
from app.db import repo as repo_module
from app.db.repo import Repo
from app.webhooks.router import router as webhook_router
from tests.fakes import FakeDevin, FakeGitHub

SECRET = "test-webhook-secret-long-enough"
REPO_FULL_NAME = "amylase/superset"
TRIGGER = "devin-fix"
OWN_LOGIN = "orchestrator-bot"


class Clock:
    """An advanceable clock, patched over every module that reads the time.

    Tests run against the **production** grace window rather than a disabled one. An earlier suite
    set `message_grace_seconds=0.0` in this file, which quietly switched off one of the system's
    guards for the whole nudge/escalate/reply surface — a real `Settings` object carrying a
    guard-disabling value is a stub by another name. Advancing time deliberately is the honest
    equivalent, and it is what lets the grace window itself be tested.
    """

    def __init__(self, monkeypatch, start: float = 1_000_000.0) -> None:
        self.t = start
        for module in (repo_module, orchestrator_module):
            monkeypatch.setattr(module, "now", lambda: self.t)

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock(monkeypatch) -> Clock:
    return Clock(monkeypatch)


@pytest.fixture
def repo(tmp_path) -> Repo:
    return Repo(str(tmp_path / "test.db"))


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Real Settings, with production values for every guard.

    A `SimpleNamespace` would hide a renamed field from every test that uses it.
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
        devin_playbook_id="playbook-xyz",
        self_login=OWN_LOGIN,
    )


@pytest.fixture
def orc(settings, clock):
    """An orchestrator wired to a real Repo, a real Effects and fake clients."""
    repo = Repo(settings.db_path)
    devin, github = FakeDevin(), FakeGitHub()
    effects = Effects(settings, repo, devin, github)
    return Orchestrator(settings, repo, effects), repo, devin, github


@pytest.fixture
def webhook_app(repo: Repo, settings: Settings) -> FastAPI:
    """The receiver mounted in isolation.

    Built without the real lifespan on purpose: the point of the receiver being side-effect free is
    that it can be exercised with no credentials, no network and no background loop. The real
    `Settings` is used so a renamed field breaks these tests too.
    """
    app = FastAPI()
    app.include_router(webhook_router)
    app.state.settings = settings
    app.state.repo = repo
    app.state.own_login = OWN_LOGIN
    return app


@pytest.fixture
def unnamed_settings() -> SimpleNamespace:
    """Marker fixture kept out of use; see `webhook_app` for why a namespace is not used."""
    raise AssertionError("use the real Settings fixture")
