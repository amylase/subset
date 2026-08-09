"""The only writer, tested directly.

The grace window had no test at all. The fixture that was supposed to run the suite against the
production window patched `now` in two modules and missed the third — `Effects`, the only module
that reads the window. `last_message_at` was written from the fake clock while `in_grace` compared
against the real one, so every message looked decades old and the guard was open in all 238 tests.
That is the same defect as setting `message_grace_seconds=0.0`, which the fixture's own docstring
claims to have fixed. These tests exist so it cannot happen a third time.
"""

from __future__ import annotations

import sys

import pytest

from app.core.effects import Effects, Reason
from app.db import repo as repo_module
from tests.conftest import Clock


def seed(repo, session_id="devin-1", number=2):
    repo.register_issue(number, "t", None)
    repo.create_session(session_id, number, url="u", tags=[], attempt=1)
    return repo.session(session_id)


# --- the clock itself --------------------------------------------------------


def test_no_module_reads_an_unpatched_clock():
    """Whoever adds `from app.db.repo import now` to a fourth module must add it to `Clock`.

    A missed module does not fail loudly. It silently disables whatever guard that module owns, and
    the suite stays green while the guard is untested — which is exactly how the grace window went
    unexercised for the whole life of the project.
    """
    readers = {
        module
        for module in list(sys.modules.values())
        if module is not None
        and getattr(module, "__name__", "").startswith("app.")
        and getattr(module, "now", None) is repo_module.now
    }
    assert readers == set(Clock.MODULES), (
        "these modules read `now` but the Clock fixture does not patch them: "
        f"{sorted(m.__name__ for m in readers - set(Clock.MODULES))}"
    )


# --- the grace window --------------------------------------------------------


@pytest.fixture
def effects(settings, repo, clock):
    from tests.fakes import FakeDevin, FakeGitHub

    return Effects(settings, repo, FakeDevin(), FakeGitHub()), repo, clock


async def test_a_message_inside_the_grace_window_is_deferred(effects):
    """A session needs a moment to act on what it was sent before its status means anything."""
    eff, repo, _ = effects
    session = seed(repo)
    assert await eff.message_session(session, reason="auto_nudge", body="one", key="k1") is True

    session = repo.session("devin-1")
    assert await eff.message_session(session, reason="auto_nudge", body="two", key="k2") is False
    assert repo.counters()["message_deferred_grace"] == 1
    assert len(eff.devin.messages) == 1


async def test_a_message_after_the_grace_window_lands(effects):
    eff, repo, clock = effects
    session = seed(repo)
    await eff.message_session(session, reason="auto_nudge", body="one", key="k1")

    clock.advance(settings_grace(eff) + 1)
    session = repo.session("devin-1")
    assert await eff.message_session(session, reason="auto_nudge", body="two", key="k2") is True
    assert len(eff.devin.messages) == 2


async def test_a_human_reply_ignores_the_grace_window(effects):
    """Genuinely new information from a person is not the loop reacting to stale state."""
    eff, repo, _ = effects
    session = seed(repo)
    await eff.message_session(session, reason="auto_nudge", body="one", key="k1")

    session = repo.session("devin-1")
    sent = await eff.message_session(
        session, reason="human_reply", body="go ahead", key="k2", respect_grace=False
    )
    assert sent is True
    assert eff.devin.messages[-1][1] == "go ahead"


def settings_grace(eff) -> float:
    return eff.settings.message_grace_seconds


# --- escalation --------------------------------------------------------------


async def test_a_reason_that_recurs_after_a_human_cleared_it_is_announced_again(effects):
    """Keyed on the reason alone, the second occurrence arrived as a label and nothing else —
    silence on the issue thread, which is the one place a human is watching."""
    eff, repo, clock = effects
    seed(repo)

    assert await eff.flag_human(2, reason=Reason.CI_UNRESOLVED, detail="first") is True
    await eff.clear_human_flag(2)
    clock.advance(600)
    assert await eff.flag_human(2, reason=Reason.CI_UNRESOLVED, detail="again") is True

    said = [body for number, body in eff.github.comments if number == 2]
    assert len(said) == 2
    assert "again" in said[1]


async def test_a_create_response_with_no_session_id_is_an_error_not_a_ghost_row(effects):
    """Recording a session with a `None` id would poll nothing, close nothing and count nothing,
    while the ACUs it is spending go on being real."""
    eff, repo, _ = effects
    repo.register_issue(2, "t", None)

    async def create_session(*args, **kwargs):
        return {"url": "https://app.devin.ai/sessions/?"}

    eff.devin.create_session = create_session
    with pytest.raises(RuntimeError, match="no session_id"):
        await eff.start_session({"number": 2}, attempt=1, prompt="p", title="t", tags=["issue:2"])
    assert repo.sessions(2) == []


async def test_the_same_reason_is_not_re_announced(effects):
    eff, repo, _ = effects
    seed(repo)
    assert await eff.flag_human(2, reason=Reason.CI_UNRESOLVED, detail="x") is True
    assert await eff.flag_human(2, reason=Reason.CI_UNRESOLVED, detail="x") is False
    assert len(eff.github.comments) == 1
