"""Configuration, loaded from the environment.

Every knob that governs cost or blast radius lives here rather than being scattered through the
code, so the operating envelope can be read in one place.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- credentials -------------------------------------------------------
    devin_api_key: str
    devin_org_id: str
    github_token: str

    #: An empty secret still produces a "valid" HMAC, so a misconfigured deployment would appear to
    #: verify signatures while accepting anything — including a forged `author_association` that
    #: defeats the whole trust model. Fail at startup instead.
    webhook_secret: str = Field(min_length=16)

    # --- targets -----------------------------------------------------------
    devin_api_base: str = "https://api.devin.ai/v3"
    github_api_base: str = "https://api.github.com"
    github_repo: str = "amylase/superset"

    #: Playbook created by ``scripts/bootstrap_devin.py``. Attaching it keeps the standing rules
    #: (diagnose first, add tests, never weaken CI, what to do about ambiguity) identical across
    #: every session and visible in the Devin dashboard rather than buried in a string literal.
    devin_playbook_id: str | None = None

    trigger_label: str = "devin-fix"
    escalation_label: str = "needs-human"

    #: The GitHub login this service writes as. Resolved at startup via `GET /user` when unset;
    #: setting it explicitly makes the identity check deterministic and survives an API hiccup.
    self_login: str | None = None

    # --- storage -----------------------------------------------------------
    db_path: str = "data/orchestrator.db"

    # --- policy: the operating envelope ------------------------------------
    max_concurrent_sessions: int = 2
    max_acu_per_session: int = 20
    global_acu_budget: float = 250.0
    max_nudges: int = 2
    max_ci_feedback_rounds: int = 3

    #: How long to leave a session alone after sending it anything. A session needs a moment to act
    #: on a message before its state means anything again; without this the loop re-reads a stale
    #: `waiting_for_user` and escalates a session it has just unblocked.
    message_grace_seconds: float = 120.0

    #: Backstop for a session that never reaches a phase the loop recognises as an ending — an
    #: unrecognised status, or one that sleeps forever having produced nothing. Without it such a
    #: session holds a concurrency slot and is polled indefinitely.
    max_session_age_hours: float = 12.0

    #: How long a pull request may sit open, green and unmerged before a human is asked to look.
    #: The session behind it has produced and is exempt from the age watchdog, so this is the only
    #: thing standing between "waiting for review" and "forgotten".
    pr_stale_hours: float = 24.0

    # --- loop cadence (seconds) -------------------------------------------
    session_poll_interval: float = 10.0
    pr_poll_interval: float = 60.0
    resync_interval: float = 300.0

    # --- reporting assumptions (surfaced on the dashboard, never hidden) ----
    acu_unit_cost_usd: float = 2.25
    manual_effort_hours_per_issue: float = 4.0
    engineer_hourly_usd: float = 100.0

    # --- admin -------------------------------------------------------------
    admin_token: str | None = None

    @property
    def repo_owner(self) -> str:
        return self.github_repo.split("/", 1)[0]

    @property
    def repo_name(self) -> str:
        return self.github_repo.split("/", 1)[1]

    @property
    def org_base(self) -> str:
        return f"{self.devin_api_base}/organizations/{self.devin_org_id}"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
