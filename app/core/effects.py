"""The only writer.

Everything that costs money or writes to GitHub happens here. The reconcile loop decides *what*
should happen; this module is *how*. No API client is called for a write anywhere else, which is
what keeps the guards in one place instead of at four call sites that each have to remember them.

**Scope.** Effects are recorded *after* they succeed. A crash between an API call and its record can
repeat that effect once — a duplicate comment, at worst a duplicate nudge. The alternative,
reserving a key beforehand, was tried and was worse: keys derived from counters another path could
reset would wedge an issue permanently, with no operator action able to clear it. A visible
duplicate beats an invisible deadlock in a system a human is watching.
"""

from __future__ import annotations

import logging
from typing import Any

from app.clients.devin import DevinClient
from app.clients.github import GitHubClient
from app.config import Settings
from app.core.state import Liveness, liveness
from app.db.repo import Repo, now

logger = logging.getLogger(__name__)


class Reason:
    """Why a human is being asked to look. One at a time, per issue."""

    BLOCKED_ON_QUESTION = "blocked_on_question"
    COST_HALT = "cost_halt"
    SESSION_ERROR = "session_error"
    SESSION_TIMEOUT = "session_timeout"
    CI_UNRESOLVED = "ci_unresolved"
    START_FAILED = "start_failed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    PR_CLOSED_UNMERGED = "pr_closed_unmerged"
    MERGE_CONFLICT = "merge_conflict"
    PR_STALE = "pr_stale"
    NOT_FIXED = "not_fixed"
    INBOX_ABANDONED = "inbox_abandoned"


class Effects:
    def __init__(
        self, settings: Settings, repo: Repo, devin: DevinClient, github: GitHubClient
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.devin = devin
        self.github = github

    def in_grace(self, session: dict[str, Any]) -> bool:
        """Whether the session was recently sent something and deserves time to act on it."""
        last = session.get("last_message_at")
        return last is not None and (now() - last) < self.settings.message_grace_seconds

    # --- Devin -------------------------------------------------------------

    async def start_session(
        self, issue: dict[str, Any], *, attempt: int, prompt: str, title: str, tags: list[str]
    ) -> dict[str, Any] | None:
        """Create a session and record it. The caller reserves the attempt number first."""
        response = await self.devin.create_session(
            prompt,
            title=title,
            tags=tags,
            repo=self.settings.github_repo,
            max_acu_limit=self.settings.max_acu_per_session,
            playbook_id=self.settings.devin_playbook_id,
        )
        session_id = response.get("session_id") if isinstance(response, dict) else None
        if not session_id:
            raise RuntimeError(f"create_session returned no session_id for #{issue['number']}")

        self.repo.create_session(
            session_id, issue["number"], url=response.get("url"), tags=tags, attempt=attempt
        )
        self.repo.bump("sessions_created")
        logger.info("session %s started for issue #%s", session_id, issue["number"])
        return self.repo.session(session_id)

    async def message_session(
        self,
        session: dict[str, Any],
        *,
        reason: str,
        body: str,
        key: str,
        respect_grace: bool = True,
        issue_number: int | None = None,
    ) -> bool:
        """The only path to ``devin.send_message``.

        Three guards, all here: the session must still be revivable, it must not be inside the
        grace window, and this exact message must not already have been sent. ``respect_grace``
        defaults to *on* — only genuinely new information from a human turns it off, because that
        is not the loop reacting to state it has not let the session update yet.
        """
        session_id = session["session_id"]
        if liveness(session) is Liveness.CLOSED:
            self.repo.bump(f"message_dropped:{reason}")
            logger.info("not messaging closed session %s (%s)", session_id, reason)
            return False
        if respect_grace and self.in_grace(session):
            self.repo.bump("message_deferred_grace")
            return False
        if self.repo.is_done(key):
            self.repo.bump("messages_deduped")
            return False

        await self.devin.send_message(session_id, body)
        self.repo.mark_done(key, f"message:{reason}")
        self.repo.mark_message_sent(session_id)
        self.repo.record_intervention(
            reason,
            session_id=session_id,
            issue_number=issue_number if issue_number is not None else session["issue_number"],
            detail=body[:500],
        )
        self.repo.bump(f"messages_sent:{reason}")
        return True

    # --- GitHub ------------------------------------------------------------

    async def comment(self, issue_number: int, *, body: str, key: str) -> bool:
        """Comment once per key. Failures are logged and counted, never fatal to the tick."""
        if self.repo.is_done(key):
            return False
        try:
            await self.github.comment(issue_number, body)
        except Exception:
            logger.exception("could not comment on #%s", issue_number)
            self.repo.bump("comment_errors")
            return False
        self.repo.mark_done(key, "comment")
        return True

    async def flag_human(
        self,
        issue_number: int,
        *,
        reason: str,
        detail: str,
        session: dict[str, Any] | None = None,
    ) -> bool:
        """Ask a human to look, and say why.

        The honesty surface: every bound, stall and failure comes through here, so a system that
        has stopped working says so rather than leaving a counter to be noticed later. Said once
        per reason — a *different* reason replaces the first and is announced, because "blocked on
        a question" and "out of credits" call for different actions.
        """
        at = self.repo.flag_for_human(issue_number, reason)
        if at is None:
            return False

        self.repo.bump(f"flagged:{reason}")
        url = session.get("url") if session else None
        body = (
            f"🙋 **Human input needed** — `{reason}`\n\n{detail}"
            + (f"\n\nSession: {url}" if url else "")
            + "\n\nReply on this issue and the answer will be forwarded to the session, if it can "
            "still be reached. Re-apply the label to start a fresh attempt."
        )
        # Keyed on *this* flagging, not on the reason. A reason that recurs after a human cleared
        # the flag is news again, and keying on the reason alone announced it with a label and
        # nothing else — silence on the thread the human is reading.
        await self.comment(
            issue_number, body=body, key=f"issue:{issue_number}:flag:{reason}:{int(at)}"
        )
        try:
            await self.github.add_label(issue_number, self.settings.escalation_label)
        except Exception:
            logger.exception("could not label #%s", issue_number)
        logger.info("flagged #%s for a human: %s", issue_number, reason)
        return True

    async def clear_human_flag(self, issue_number: int) -> None:
        if not self.repo.clear_human_flag(issue_number):
            return
        try:
            await self.github.remove_label(issue_number, self.settings.escalation_label)
        except Exception:
            logger.debug("could not remove the escalation label from #%s", issue_number)
