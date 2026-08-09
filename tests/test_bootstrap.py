"""The bootstrap script, which had no tests and is the sole registrar of two paid resources.

Both dedupe checks here read a key out of a list response, and both have already been wrong once:
the playbook API rejects `name` and wants `title`, the schedules API does the opposite. Reading the
wrong key does not fail — it makes the dedupe never match, so every run registers *another*
recurring weekly session that spends ACUs forever with nobody watching.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.core.prompts import PLAYBOOK_TITLE
from scripts import bootstrap_devin


class FakeDevin:
    def __init__(self, playbooks: list[dict] | None = None, schedules: list[dict] | None = None):
        self.playbooks = playbooks or []
        self.schedules = schedules or []
        self.created_playbooks: list[tuple[str, str]] = []
        self.created_schedules: list[tuple[str, str, str]] = []
        self.closed = False

    async def list_playbooks(self) -> Any:
        return {"items": self.playbooks}

    async def create_playbook(self, title: str, body: str) -> Any:
        self.created_playbooks.append((title, body))
        return {"playbook_id": "playbook-new"}

    async def list_schedules(self) -> Any:
        return {"items": self.schedules}

    async def create_schedule(self, name: str, prompt: str, frequency: str) -> Any:
        self.created_schedules.append((name, prompt, frequency))
        return {"scheduled_session_id": "sched-new"}

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def bootstrap(monkeypatch):
    """Runs `main` against a double, and hands back what it did."""
    settings = SimpleNamespace(
        devin_api_key="cog_test",
        org_base="https://api.devin.test/v3/organizations/org-1",
        github_repo="amylase/superset",
    )
    monkeypatch.setattr(bootstrap_devin, "get_settings", lambda: settings)

    def run(fake: FakeDevin, *, schedule: bool):
        import asyncio

        monkeypatch.setattr(bootstrap_devin, "DevinClient", lambda *a, **k: fake)
        assert asyncio.run(bootstrap_devin.main(schedule)) == 0
        assert fake.closed, "the client must be closed even when nothing was created"
        return fake

    return run


def test_the_playbook_is_created_when_it_is_absent(bootstrap):
    fake = bootstrap(FakeDevin(), schedule=False)
    assert [title for title, _ in fake.created_playbooks] == [PLAYBOOK_TITLE]


def test_an_existing_playbook_is_recognised_by_its_title(bootstrap):
    """`title`, not `name` — the create call is rejected with a 422 for `name`, but the *list*
    call would simply never match, and a second playbook would be registered on every run."""
    fake = bootstrap(FakeDevin(playbooks=[{"title": PLAYBOOK_TITLE}]), schedule=False)
    assert fake.created_playbooks == []


def test_no_schedule_is_created_without_the_flag(bootstrap):
    """A recurring schedule spends ACUs every week whether or not anyone is watching."""
    fake = bootstrap(FakeDevin(), schedule=False)
    assert fake.created_schedules == []


def test_the_weekly_schedule_is_created_once(bootstrap):
    fake = bootstrap(FakeDevin(), schedule=True)
    assert len(fake.created_schedules) == 1
    name, prompt, frequency = fake.created_schedules[0]
    assert name == "Weekly audit: amylase/superset"
    assert frequency == "weekly"
    assert "amylase/superset" in prompt


def test_an_existing_schedule_is_recognised_by_its_name(bootstrap):
    """The list response names schedules `name`. Reading `title` here matched nothing, so every
    run added another weekly recurring spend — the failure is silent and it compounds."""
    rows = [{"name": "Weekly audit: amylase/superset", "title": "something else"}]
    fake = bootstrap(FakeDevin(schedules=rows), schedule=True)
    assert fake.created_schedules == []


def test_an_unexpected_list_shape_does_not_crash_the_run(bootstrap):
    """A shape change degrades to "register it again", which is visible, rather than an exception
    that leaves the playbook unregistered and every session running without its standing rules."""
    fake = FakeDevin()
    fake.list_playbooks = _returning(None)  # type: ignore[method-assign]
    fake.list_schedules = _returning("not a collection")  # type: ignore[method-assign]
    bootstrap(fake, schedule=True)
    assert len(fake.created_playbooks) == 1
    assert len(fake.created_schedules) == 1


def _returning(value: Any):
    async def call() -> Any:
        return value

    return call
