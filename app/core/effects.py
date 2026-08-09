"""The only writer.

Everything that costs money, writes to GitHub, or talks to Devin happens here. The reconcile loop
decides *what* should happen; this module is *how*, exactly once.

That split is the central lesson from v1. There, four call sites sent messages to a session and each
needed the same three guards — is the session still revivable, has it just been sent something, has
this exact message already been sent. Two reviews found a different one of those guards missing each
time. With four independent paths there was no way to be right; with one there is nowhere to put a
mistake.

Every effect follows claim -> act -> confirm against the ledger in ``app.db.repo``. The claim lands
*before* the action, so a crash mid-flight leaves the key held and the effect is not retried — the
safe direction when retrying means spending money or writing to GitHub twice. A failure that
provably did nothing releases the key; see :meth:`_perform`.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.clients.devin import DevinClient
from app.clients.github import GitHubClient
from app.clients.http import ApiError
from app.config import Settings
from app.core.state import Liveness, liveness
from app.db.repo import Repo, now

logger = logging.getLogger(__name__)


class AmbiguousEffect(RuntimeError):
    """An effect failed in a way that leaves it unknown whether it happened.

    The ledger key stays claimed, so the caller must not retry. The loop turns this into a
    notification instead: telling a human that a message may not have been delivered is honest,
    where retrying would risk doing it twice.
    """

    def __init__(self, key: str, kind: str, cause: Exception) -> None:
        super().__init__(f"{kind} ({key}) failed ambiguously: {cause!r}")
        self.key = key
        self.kind = kind
        self.cause = cause


class Reason:
    """Notification reason classes.

    One open notification per class per issue, so a cost halt following a question escalation is
    still reported — v1 deduped on issue state and swallowed the second, different reason.
    """

    BLOCKED_ON_QUESTION = "blocked_on_question"
    COST_HALT = "cost_halt"
    SESSION_ERROR = "session_error"
    SESSION_TIMEOUT = "session_timeout"
    CI_UNRESOLVED = "ci_unresolved"
    START_FAILED = "start_failed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    UNDELIVERABLE = "undeliverable_message"
    PR_CLOSED_UNMERGED = "pr_closed_unmerged"


class Effects:
    def __init__(
        self, settings: Settings, repo: Repo, devin: DevinClient, github: GitHubClient
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.devin = devin
        self.github = github

    # --- the protocol ------------------------------------------------------

    async def _perform(self, key: str, kind: str, action: Callable[[], Awaitable[Any]]) -> bool:
        """claim -> act -> confirm. ``False`` if the key was already claimed."""
        if not self.repo.claim_effect(key, kind):
            self.repo.bump("effects_deduped")
            return False
        try:
            await action()
        except ApiError as exc:
            if exc.status < 500:
                # Rejected, so nothing happened outward and a later attempt is safe.
                self.repo.release_effect(key)
                raise
            raise AmbiguousEffect(key, kind, exc) from exc
        except Exception as exc:
            # 5xx, a timeout, a dropped connection, a bug: it is not known whether the action
            # happened. The key stays held, so this is never retried — re-sending a message that
            # may already have landed, or paying for a session twice, is worse than stopping.
            raise AmbiguousEffect(key, kind, exc) from exc
        self.repo.confirm_effect(key)
        return True

    def in_grace(self, session: dict[str, Any]) -> bool:
        """Whether the session was recently sent something and deserves time to act on it.

        Consulted by the loop before it *decides* anything from a session's state, not only before
        it sends. v1 checked an equivalent at exactly one call site and the escalation path went
        around it, so forwarding a human's answer was followed at once by another escalation.
        """
        last = session.get("last_message_at")
        if last is None:
            return False
        return (now() - last) < self.settings.message_grace_seconds

    # --- Devin -------------------------------------------------------------

    async def start_session(
        self, issue: dict[str, Any], *, attempt: int, prompt: str, title: str, tags: list[str]
    ) -> dict[str, Any] | None:
        """Create a session. Returns the record, or ``None`` if this attempt was already made."""
        key = f"issue:{issue['number']}:attempt:{attempt}"
        response: dict[str, Any] = {}

        async def act() -> None:
            nonlocal response
            response = await self.devin.create_session(
                prompt,
                title=title,
                tags=tags,
                repo=self.settings.github_repo,
                max_acu_limit=self.settings.max_acu_per_session,
                playbook_id=self.settings.devin_playbook_id,
            )

        if not await self._perform(key, "start_session", act):
            return None

        session_id = response.get("session_id") if isinstance(response, dict) else None
        if not session_id:
            raise RuntimeError(f"create_session returned no session_id for #{issue['number']}")

        self.repo.create_session(
            session_id,
            issue["number"],
            url=response.get("url"),
            tags=tags,
            attempt=attempt,
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
        respect_grace: bool = False,
        issue_number: int | None = None,
    ) -> bool:
        """The only path to ``devin.send_message``.

        All three guards live here, so a fifth reason to message a session cannot reintroduce a
        missing one — there is nowhere else to put it.
        """
        session_id = session["session_id"]
        if liveness(session) is Liveness.CLOSED:
            self.repo.bump(f"message_dropped:{reason}")
            logger.info("not messaging closed session %s (%s)", session_id, reason)
            return False
        if respect_grace and self.in_grace(session):
            self.repo.bump("message_deferred_grace")
            return False

        async def act() -> None:
            await self.devin.send_message(session_id, body)

        if not await self._perform(key, f"message:{reason}", act):
            return False

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
        async def act() -> None:
            await self.github.comment(issue_number, body)

        try:
            return await self._perform(key, "comment", act)
        except Exception:
            logger.exception("could not comment on #%s", issue_number)
            self.repo.bump("comment_errors")
            return False

    async def notify(
        self,
        issue_number: int,
        *,
        reason_class: str,
        detail: str,
        session: dict[str, Any] | None = None,
    ) -> bool:
        """Open a notification and tell the humans, once per reason class.

        This is the honesty surface: every bound, stall and failure comes through here, so a system
        that has stopped working says so instead of leaving a counter to be noticed later.
        """
        if not self.repo.open_notification(
            issue_number,
            reason_class,
            session_id=session["session_id"] if session else None,
            detail=detail,
        ):
            return False

        self.repo.bump(f"notifications:{reason_class}")
        url = session.get("url") if session else None
        session_line = f"\n\nSession: {url}" if url else ""
        body = (
            f"🙋 **Human input needed** — `{reason_class}`\n\n{detail}{session_line}\n\n"
            "Reply on this issue and the answer will be forwarded to the session. "
            "Re-apply the label to start a fresh attempt."
        )
        await self.comment(
            issue_number, body=body, key=f"issue:{issue_number}:notify:{reason_class}"
        )
        try:
            await self.github.add_label(issue_number, self.settings.escalation_label)
        except Exception:
            logger.exception("could not label #%s", issue_number)
        logger.info("notified #%s: %s", issue_number, reason_class)
        return True

    async def resolve(self, issue_number: int, reason_class: str | None = None) -> None:
        """Close notifications and drop the label once none remain open."""
        if not self.repo.resolve_notifications(issue_number, reason_class):
            return
        if self.repo.open_notifications(issue_number):
            return
        try:
            await self.github.remove_label(issue_number, self.settings.escalation_label)
        except Exception:
            logger.debug("could not remove the escalation label from #%s", issue_number)
