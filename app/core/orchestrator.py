"""The reconcile loop.

Derives status from facts, decides what should change, and asks :mod:`app.core.effects` to change
it. It performs no writes of its own, which keeps the concurrency, ACU and message guards
enforceable in one place.

Five passes at different cadences inside a single tick: drain the inbox, start sessions, track
sessions, track pull requests (60s), resync and refresh analytics (300s).

Ticks are serialised. The admin endpoint runs ``tick()`` on the same event loop as the background
task, and without the lock two ticks interleave at the ``await`` inside session creation — two paid
sessions for one issue, and the concurrency cap bypassed.

Resync is not redundant with webhooks: GitHub does not retry failed deliveries, so an event
arriving while this process is down is gone for good.

**Scope.** A human watches this run. It is allowed to fail visibly and be restarted; it does not try
to guarantee exactly-once effects across a crash.

Two properties it does hold to, because both were broken once and neither failure was visible:

* **It does not spend twice for one attempt.** The attempt is reserved before the billable call, an
  ambiguous `POST` is never repeated (`app.clients.http`), and a retry supersedes the session it
  replaces rather than running alongside it.
* **It does not stall invisibly.** Every dead end reaches `flag_human`: a session that produced no
  pull request whatever its stated outcome, a pull request that never moves, feedback that cannot
  reach a closed session, an inbox item given up on, an exhausted nudge or CI budget. A counter is
  not an escalation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from app.clients.devin import collection_items
from app.config import Settings
from app.core import prompts
from app.core.effects import Effects, Reason
from app.core.state import (
    IssueStatus,
    Liveness,
    closing_reason,
    has_produced,
    is_blocked,
    is_final_output,
    issue_status,
    liveness,
    occupies_slot,
)
from app.db.repo import Repo, now

logger = logging.getLogger(__name__)

_PR_URL = re.compile(r"/pull/(\d+)$")

#: Tag on every session this orchestrator creates. Reviewers cross-check the tags visible in the
#: Devin dashboard against what the orchestrator claims to send, so these are real identifiers only.
ORCHESTRATOR_TAG = "orchestrator:superset-remediation"

MAX_INBOX_ATTEMPTS = 3


def parse_pr_number(url: str | None, repo: str) -> int | None:
    """Extract a pull request number, but only for our own repository.

    Anchored on purpose: a loose ``/pull/(\\d+)`` search accepts a URL for any repository, and the
    orchestrator would then act on that number against ours. The trailing ``/pull/`` is part of the
    anchor because a bare prefix also matches any repository whose name merely *starts* with ours —
    ``amylase/superset-fork`` would be read as ``amylase/superset``.
    """
    if not url:
        return None
    if not url.startswith(f"https://github.com/{repo}/pull/"):
        return None
    match = _PR_URL.search(url)
    return int(match.group(1)) if match else None


class Orchestrator:
    def __init__(self, settings: Settings, repo: Repo, effects: Effects) -> None:
        self.settings = settings
        self.repo = repo
        self.effects = effects
        self.devin = effects.devin
        self.github = effects.github
        self._tick = 0
        self._lock = asyncio.Lock()

    # -- lifecycle ----------------------------------------------------------

    async def run_forever(self) -> None:
        interval = self.settings.session_poll_interval
        pr_every = max(1, round(self.settings.pr_poll_interval / interval))
        slow_every = max(1, round(self.settings.resync_interval / interval))

        while True:
            try:
                await self.tick(pr_every=pr_every, slow_every=slow_every)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("reconcile tick failed")
                self.repo.bump("tick_errors")
            await asyncio.sleep(interval)

    async def tick(self, *, pr_every: int = 6, slow_every: int = 30) -> None:
        async with self._lock:
            self._tick += 1
            await self.drain_inbox()
            await self.start_sessions()
            await self.track_sessions()
            if self._tick % pr_every == 0:
                await self.track_pull_requests()
            if self._tick % slow_every == 0:
                await self.resync()
                await self.refresh_insights()

    # -- the shared view ----------------------------------------------------

    def contexts(self) -> list[dict[str, Any]]:
        """Every issue with its sessions, pull requests and derived status.

        The control plane and the reporting surface read the same thing. When they did not, an
        issue could sit in a status the dashboard displayed and nothing acted on.
        """
        rows = []
        for issue in self.repo.issues():
            sessions = self.repo.sessions(issue["number"])
            pulls = self.repo.pull_requests(issue["number"])
            rows.append(
                {
                    "issue": issue,
                    "sessions": sessions,
                    "pulls": pulls,
                    "status": issue_status(issue, sessions, pulls),
                }
            )
        return rows

    # -- pass 1: recorded intent -------------------------------------------

    async def drain_inbox(self) -> None:
        for item in self.repo.pending_inbox():
            try:
                await self._handle(item["kind"], item["payload"])
            except Exception as exc:
                logger.exception("inbox item %s (%s) failed", item["id"], item["kind"])
                self.repo.bump("inbox_errors")
                if self.repo.record_inbox_failure(
                    item["id"], repr(exc), max_attempts=MAX_INBOX_ATTEMPTS
                ):
                    self.repo.bump("inbox_abandoned")
                    await self._abandoned(item, exc)
            else:
                self.repo.mark_dispatched(item["id"])

    async def _abandoned(self, item: dict[str, Any], exc: Exception) -> None:
        """Say out loud that a recorded event was given up on.

        The inbox is the only copy of a human's answer or a reviewer's comment — GitHub does not
        redeliver. Dropping one after three failures and moving on left a person waiting for a
        reply that would never come, with nothing but a counter to show for it.
        """
        number = self._issue_for(item["kind"], item["payload"])
        if number is None:
            logger.error("abandoned inbox item %s (%s): %r", item["id"], item["kind"], exc)
            return
        await self.effects.flag_human(
            number,
            reason=Reason.INBOX_ABANDONED,
            detail=(
                f"A `{item['kind']}` event could not be processed after {MAX_INBOX_ATTEMPTS} "
                f"attempts and has been given up on: `{exc!r}`. Nothing was acted on, so if this "
                "was a reply to Devin it did not reach the session."
            ),
        )

    def _issue_for(self, kind: str, payload: dict[str, Any]) -> int | None:
        if kind == "issue_labeled":
            return payload.get("number")
        if kind == "issue_comment":
            return payload.get("issue_number")
        record = self.repo.pull_request(payload.get("pr_number", -1))
        return record["issue_number"] if record else None

    async def _handle(self, kind: str, payload: dict[str, Any]) -> None:
        match kind:
            case "issue_labeled":
                await self._register_issue(payload["number"])
            case "ci_failed":
                await self._feed_ci_failure(payload["pr_number"], payload.get("sha"))
            case "review_comment":
                await self._forward_review(
                    payload["pr_number"],
                    payload["author"],
                    payload["comment"],
                    payload["comment_id"],
                )
            case "issue_comment":
                await self._forward_reply(
                    payload["issue_number"],
                    payload["author"],
                    payload["comment"],
                    payload["comment_id"],
                )
            case "pr_closed":
                record = self.repo.pull_request(payload["pr_number"])
                if record:
                    await self._advance_pull_request(record)
            case _:
                raise ValueError(f"no handler for inbox kind: {kind}")

    async def _register_issue(self, number: int) -> None:
        """Trust-but-verify: confirm the label is really on the issue before spending anything."""
        issue = await self.github.get_issue(number)
        labels = {label["name"] for label in issue.get("labels", [])}
        if self.settings.trigger_label not in labels or issue.get("state") != "open":
            logger.info("#%s is not an open labeled issue; ignoring", number)
            self.repo.bump("stale_events_ignored")
            return

        klass = next((label for label in labels if label.startswith("class:")), None)
        if self.repo.register_issue(number, issue.get("title", ""), klass):
            logger.info("registered issue #%s (%s)", number, klass)
            return

        # Already known: re-applying the label means "try again". One fact; the derived status
        # picks it up on the next pass.
        self.repo.request_retry(number)
        self.repo.bump("retries_requested")
        await self.effects.clear_human_flag(number)
        await self._supersede_open_sessions(number)
        logger.info("retry requested for issue #%s", number)

    async def _supersede_open_sessions(self, number: int) -> None:
        """Stop tracking any session still open on an issue that is about to be retried.

        A retry outranks a running session on purpose (`state.wants_session`), which without this
        left the previous session billing alongside the new one, holding a concurrency slot and free
        to open a competing pull request. The v3 API exposes no way to terminate a session — probed:
        `/terminate` and `/stop` are both 404 — so this is bookkeeping, not a kill. What it buys is
        real: the session stops being messaged, stops holding a slot, and is marked in the audit
        trail as superseded rather than silently doubling the spend. It still costs whatever it
        spends before it sleeps, which is why the per-session ACU cap exists, and the Analytics
        refresh keeps counting it against the global budget.
        """
        for record in self.repo.sessions(number):
            if liveness(record) is Liveness.CLOSED or record["produced_at"] is not None:
                # A session that already delivered is left alone: its pull request keeps the issue
                # in `pr_open`, no new session starts, and closing it here would sever the
                # review-fix loop that feeds CI failures back to it.
                continue
            if self.repo.close_session(record["session_id"], "superseded"):
                self.repo.bump("sessions_superseded")
                logger.info("session %s superseded by a retry of #%s", record["session_id"], number)

    # -- pass 2: start sessions --------------------------------------------

    async def start_sessions(self) -> None:
        contexts = self.contexts()
        active = sum(
            1
            for ctx in contexts
            for s in ctx["sessions"]
            if occupies_slot(s, issue_is_terminal=ctx["status"] is IssueStatus.MERGED)
        )
        spent = self.repo.total_acus()

        for ctx in contexts:
            if ctx["status"] is not IssueStatus.QUEUED:
                continue
            issue = ctx["issue"]

            if active >= self.settings.max_concurrent_sessions:
                self.repo.bump("start_deferred_concurrency")
                return
            if spent >= self.settings.global_acu_budget:
                await self.effects.flag_human(
                    issue["number"],
                    reason=Reason.BUDGET_EXHAUSTED,
                    detail=(
                        f"The global ACU budget ({self.settings.global_acu_budget}) is spent, so "
                        "no further sessions will start. Raise `GLOBAL_ACU_BUDGET` to continue."
                    ),
                )
                return

            active += 1
            # Count what this session is *allowed* to spend, not what it has spent. A fresh session
            # reports 0 ACU until its first poll, so a budget read once per tick let a whole tick's
            # worth of sessions start against a figure none of them had moved yet.
            spent += self.settings.max_acu_per_session
            try:
                await self._create_session(issue)
            except Exception:
                logger.exception("starting a session for #%s failed", issue["number"])
                self.repo.bump("session_start_errors")
                await self.effects.flag_human(
                    issue["number"],
                    reason=Reason.START_FAILED,
                    detail=(
                        "Creating a Devin session failed. The attempt is recorded, so nothing will "
                        "be billed twice; check the Devin dashboard, then re-apply the label to "
                        "try again."
                    ),
                )

    async def _create_session(self, issue: dict[str, Any]) -> None:
        number = issue["number"]
        # Reserve the attempt before anything billable. A failure after this point cannot lead to a
        # second session for the same attempt, and a later re-label is still recognised as a retry.
        attempt = self.repo.begin_attempt(number)

        tags = [ORCHESTRATOR_TAG, f"repo:{self.settings.github_repo}", f"issue:{number}"]
        if issue.get("klass"):
            tags.append(issue["klass"])

        session = await self.effects.start_session(
            issue,
            attempt=attempt,
            prompt=prompts.session_prompt(
                repo=self.settings.github_repo,
                issue_number=number,
                issue_title=issue.get("title", ""),
                issue_url=f"https://github.com/{self.settings.github_repo}/issues/{number}",
            ),
            title=f"superset#{number}: {issue.get('title', '')}"[:120],
            tags=tags,
        )
        if session is None:
            return

        await self.effects.comment(
            number,
            body=(
                "🤖 Devin session started for this issue.\n\n"
                f"- Session: {session['url']}\n"
                f"- Tags: `{'`, `'.join(tags)}`\n"
                f"- ACU cap: {self.settings.max_acu_per_session}\n\n"
                "Progress will be reported here."
            ),
            key=f"session:{session['session_id']}:started",
        )

    # -- pass 3: track sessions --------------------------------------------

    async def track_sessions(self) -> None:
        max_age = self.settings.max_session_age_hours * 3600
        for ctx in self.contexts():
            # A merged issue is settled, so nothing here acts on it — but its sessions are still
            # polled. One that is somehow still running is spending real money, and dropping it
            # from the poll meant the last figure recorded for it was whatever it happened to
            # report before the merge.
            settled = ctx["status"] is IssueStatus.MERGED
            for record in ctx["sessions"]:
                if liveness(record) is Liveness.CLOSED:
                    continue
                # The backstop for anything the status vocabulary does not express: an unrecognised
                # status, or a session that rests indefinitely having produced nothing. A session
                # that *has* produced is exempt: it is sleeping next to an open pull request,
                # waiting for CI or a reviewer, and closing it there would both raise a false alarm
                # and sever the review-fix loop. The pull request's own staleness bound covers it.
                if record["produced_at"] is None and now() - record["created_at"] > max_age:
                    self.repo.close_session(record["session_id"], "timeout")
                    self.repo.bump("sessions_timed_out")
                    await self.effects.flag_human(
                        record["issue_number"],
                        reason=Reason.SESSION_TIMEOUT,
                        detail=(
                            "The session has been open for over "
                            f"{self.settings.max_session_age_hours:.0f}h and is no longer tracked."
                        ),
                        session=record,
                    )
                    continue
                try:
                    await self._advance_session(record, act=not settled)
                except Exception:
                    logger.exception("advancing session %s failed", record["session_id"])

    async def _advance_session(self, record: dict[str, Any], *, act: bool = True) -> None:
        session_id = record["session_id"]
        remote = await self.devin.get_session(session_id)

        status = remote.get("status")
        detail = remote.get("status_detail")
        structured = remote.get("structured_output")
        pulls = remote.get("pull_requests") or []
        observed = {**record, "status": status, "status_detail": detail}

        produced = has_produced(
            observed,
            has_structured_output=is_final_output(structured),
            has_pull_request=bool(pulls),
        )
        reason = closing_reason(observed)

        if self.repo.record_poll(
            session_id,
            status=status,
            status_detail=detail,
            acus=float(remote.get("acus_consumed") or 0),
            structured_output=structured,
            blocked=is_blocked(observed),
            produced=produced,
            closed_reason=reason,
        ):
            logger.info("session %s -> %s/%s", session_id, status, detail)

        if pulls:
            self._link_pull_requests(record, pulls)

        current = self.repo.session(session_id) or record
        if not act:
            # The issue is settled. The poll above is what we came for: an accurate status and an
            # accurate ACU figure. Everything below writes to GitHub or spends, so it stops here.
            return

        if produced:
            await self._report_completion(current, structured, pulls)
            outcome = structured.get("outcome") if isinstance(structured, dict) else None
            if not pulls:
                # Any outcome without a pull request needs a human, including `fixed`. Restricting
                # this to the two admitted failures let the worst version through untouched: a
                # session reporting success with nothing to show for it, parked in `exhausted`.
                await self.effects.flag_human(
                    record["issue_number"],
                    reason=Reason.NOT_FIXED,
                    detail=f"Devin reported `{outcome}` and opened no pull request.",
                    session=current,
                )
            return

        if reason and reason.startswith("cost_halt"):
            await self.effects.flag_human(
                record["issue_number"],
                reason=Reason.COST_HALT,
                detail=f"Devin stopped: `{detail}`. No message can revive this session.",
                session=current,
            )
        elif reason:
            await self.effects.flag_human(
                record["issue_number"],
                reason=Reason.SESSION_ERROR,
                detail=(
                    f"The session ended (`{status}`) without producing a pull request or a final "
                    "result. Re-apply the label to try again."
                ),
                session=current,
            )
        elif is_blocked(observed) and not self.effects.in_grace(current):
            await self._handle_blocked(current)

    def _link_pull_requests(self, record: dict[str, Any], pulls: list[dict[str, Any]]) -> None:
        for pull in pulls:
            number = parse_pr_number(pull.get("pr_url"), self.settings.github_repo)
            if number is None:
                self.repo.bump("foreign_pr_url_ignored")
                continue
            if self.repo.upsert_pr(
                number,
                issue_number=record["issue_number"],
                session_id=record["session_id"],
                url=pull.get("pr_url"),
                opened_at=now(),
            ):
                self.repo.bump("prs_opened")

    async def _handle_blocked(self, session: dict[str, Any]) -> None:
        spent = session["nudges"] - session["nudge_base"]
        if spent < self.settings.max_nudges:
            # Keyed on the ordinal, which only ever increases — a human reply moves `nudge_base`,
            # never `nudges`. When the reset rewound the counter instead, the key regenerated to one
            # already recorded as done, every later nudge was dropped as a duplicate, the budget
            # never advanced, and the escalation below became unreachable for the life of the
            # session.
            sent = await self.effects.message_session(
                session,
                reason="auto_nudge",
                body=prompts.nudge_message(),
                key=f"session:{session['session_id']}:nudge:{session['nudges'] + 1}",
            )
            if sent:
                self.repo.bump_nudges(session["session_id"])
            return

        question = ""
        try:
            question = await self.devin.latest_devin_message(session["session_id"])
        except Exception:
            logger.debug("could not read messages for %s", session["session_id"], exc_info=True)
        await self.effects.flag_human(
            session["issue_number"],
            reason=Reason.BLOCKED_ON_QUESTION,
            detail=(
                f"Devin is blocked on a question and the automatic nudge limit "
                f"({self.settings.max_nudges}) is exhausted."
                + (f"\n\n> {question}" if question else "")
            ),
            session=session,
        )

    async def _report_completion(
        self, session: dict[str, Any], structured: Any, pulls: list[dict[str, Any]]
    ) -> None:
        data = structured if isinstance(structured, dict) else {}
        lines = [f"✅ **Devin finished** — outcome: `{data.get('outcome', 'unknown')}`", ""]
        if data.get("root_cause"):
            lines += [f"**Root cause.** {data['root_cause']}", ""]
        if data.get("summary"):
            lines += [data["summary"], ""]
        for pull in pulls:
            lines.append(f"- Pull request: {pull.get('pr_url')} (`{pull.get('pr_state')}`)")
        for heading, items in (
            ("Tests added", data.get("tests_added") or []),
            ("Assumptions", data.get("assumptions") or []),
        ):
            if items:
                lines += ["", f"**{heading}.**"] + [f"- {item}" for item in items]
        if data.get("follow_up"):
            lines += ["", f"**Left undone.** {data['follow_up']}"]
        lines += ["", f"ACUs consumed: {session['acus']:.2f} · Session: {session['url']}"]

        if await self.effects.comment(
            session["issue_number"],
            body="\n".join(lines),
            key=f"session:{session['session_id']}:completion",
        ):
            self.repo.bump("completions_reported")

    # -- pass 4: pull request outcomes -------------------------------------

    async def track_pull_requests(self) -> None:
        for record in self.repo.tracked_pull_requests():
            try:
                await self._advance_pull_request(record)
            except Exception:
                logger.exception("advancing PR #%s failed", record["pr_number"])

    async def _advance_pull_request(self, record: dict[str, Any]) -> None:
        number = record["pr_number"]
        pull = await self.github.get_pull(number)

        if pull.get("merged_at"):
            if record["merged_at"] is None:
                self.repo.update_pr(number, merged_at=now())
                self.repo.bump("prs_merged")
                if record["issue_number"]:
                    await self.effects.clear_human_flag(record["issue_number"])
                logger.info("PR #%s merged", number)
            return
        if pull.get("state") == "closed":
            if record["closed_at"] is None:
                self.repo.update_pr(number, closed_at=now())
                self.repo.bump("prs_closed_unmerged")
                if record["issue_number"]:
                    await self.effects.flag_human(
                        record["issue_number"],
                        reason=Reason.PR_CLOSED_UNMERGED,
                        detail=(
                            f"{record['url']} was closed without merging, so this issue is not "
                            "fixed. Re-apply the label to try again."
                        ),
                    )
            return

        sha = pull["head"]["sha"]
        settled, conclusion = await self.github.checks_settled(sha)
        if settled and record["ci_settled_sha"] != sha:
            # Stamped once per commit. After a self-correction round the earlier verdict is stale,
            # so a new head sha re-stamps — but a stamp rewritten on every poll tracks *now*, and
            # the CI slice then swallowed however long a human took to review.
            self.repo.update_pr(
                number, ci_settled_at=now(), ci_settled_sha=sha, ci_conclusion=conclusion
            )
        elif settled and record["ci_conclusion"] != conclusion:
            self.repo.update_pr(number, ci_conclusion=conclusion)
        if settled and conclusion == "failure":
            await self._feed_ci_failure(number, sha)
            return

        opened = record["opened_at"]
        if opened is not None and now() - opened > self.settings.pr_stale_hours * 3600:
            # Neither merged nor closed nor failing, for long enough that nothing is going to move
            # it without a person. Without this bound an issue sits in `pr_open` forever: its
            # session has produced, so the session watchdog exempts it, and nothing else is
            # watching.
            await self.effects.flag_human(
                record["issue_number"],
                reason=Reason.PR_STALE,
                detail=(
                    f"{record['url']} has been open for over "
                    f"{self.settings.pr_stale_hours:.0f}h without merging. Review it, or close it "
                    "and re-apply the label to try again."
                ),
            )

    # -- the review-fix loop ------------------------------------------------

    async def _feed_ci_failure(self, pr_number: int, sha: str | None) -> None:
        record = self.repo.pull_request(pr_number)
        if not record or not record["session_id"]:
            return
        session = self.repo.session(record["session_id"])
        if not session:
            return

        if not sha:
            pull = await self.github.get_pull(pr_number)
            sha = pull["head"]["sha"]
        if record["ci_last_sha"] == sha:
            # One round per commit. Without this the whole budget goes on re-reporting the same red
            # commit on consecutive polls, minutes apart, before Devin can push anything.
            self.repo.bump("ci_feedback_deduped")
            return

        if record["ci_rounds"] - record["ci_rounds_base"] >= self.settings.max_ci_feedback_rounds:
            await self._flag_ci_unresolved(record, session)
            return

        failed = await self.github.failed_check_summary(sha)
        if not failed:
            # Settled-as-failure with nothing named means the two reads disagreed transiently.
            # Nothing is recorded, so the retry fires once the names appear.
            self.repo.bump("ci_failure_without_detail")
            return

        rounds = record["ci_rounds"] + 1
        if await self.effects.message_session(
            session,
            reason="ci_feedback",
            body=prompts.ci_failure_message(
                pr_url=record["url"] or "", failed_checks=failed, round_number=rounds
            ),
            key=f"pr:{pr_number}:ci:{sha}",
            issue_number=record["issue_number"],
        ):
            self.repo.update_pr(pr_number, ci_rounds=rounds, ci_last_sha=sha)
            logger.info("handed CI failure back for PR #%s (round %s)", pr_number, rounds)
        else:
            # The message did not land — the session is closed, or it is inside the grace window.
            # A closed session can never receive it, so that is a dead end a human must see.
            if liveness(session) is Liveness.CLOSED:
                await self._flag_ci_unresolved(record, session)

    async def _flag_ci_unresolved(self, record: dict[str, Any], session: dict[str, Any]) -> None:
        await self.effects.flag_human(
            record["issue_number"],
            reason=Reason.CI_UNRESOLVED,
            detail=(
                f"CI is failing on {record['url']} and Devin cannot take it further. Note that "
                "some checks on this fork are flaky or cannot run at all — see the CI baseline in "
                "the README before assuming the code is at fault."
            ),
            session=session,
        )

    async def _forward_review(
        self, pr_number: int, author: str, comment: str, comment_id: int
    ) -> None:
        """Send reviewer feedback to the session that produced *this* pull request."""
        record = self.repo.pull_request(pr_number)
        if not record or not record["session_id"]:
            self.repo.bump("review_comment_unmatched")
            return
        session = self.repo.session(record["session_id"])
        if not session:
            return

        # Namespaced: issue-comment ids and pull-request-review-comment ids are separate GitHub
        # sequences and do collide numerically, which under one key format silently dropped
        # whichever arrived second as a duplicate.
        if await self.effects.message_session(
            session,
            reason="review_feedback",
            body=prompts.review_feedback_message(
                pr_url=record["url"] or "", reviewer=author, comment=comment
            ),
            key=f"review_comment:{comment_id}",
            respect_grace=False,
            issue_number=record["issue_number"],
        ):
            return
        if liveness(session) is Liveness.CLOSED:
            # Symmetric with a human reply that cannot be delivered: the reviewer is waiting for a
            # response that is never coming, so say so where they are looking.
            self.repo.bump("review_feedback_undeliverable")
            await self.effects.comment(
                record["issue_number"] or pr_number,
                body=(
                    f"⚠️ Review feedback on {record['url']} could not be delivered: the Devin "
                    "session that opened it is closed and cannot be revived. Re-apply the label to "
                    "start a fresh attempt."
                ),
                key=f"review_comment:{comment_id}:undeliverable",
            )

    async def _forward_reply(
        self, issue_number: int, author: str, comment: str, comment_id: int
    ) -> None:
        """Forward a human's answer to the session working on this issue.

        Keyed on the comment id, so a second reply is delivered rather than dropped and delivery
        does not depend on the issue being in a particular state.
        """
        session = self.repo.latest_session_for_issue(issue_number)
        if not session:
            return

        sent = await self.effects.message_session(
            session,
            reason="human_reply",
            body=prompts.human_reply_message(author=author, comment=comment),
            key=f"issue_comment:{comment_id}",
            respect_grace=False,
        )
        if sent:
            # A human took over and handed back, so the automatic budgets start again.
            self.repo.reset_budgets(session["session_id"])
            await self.effects.clear_human_flag(issue_number)
        elif liveness(session) is Liveness.CLOSED:
            self.repo.bump("human_reply_undeliverable")
            await self.effects.comment(
                issue_number,
                body=(
                    "⚠️ That reply could not be delivered: the Devin session is closed and cannot "
                    "be revived. Re-apply the label to start a fresh attempt."
                ),
                key=f"issue_comment:{comment_id}:undeliverable",
            )

    # -- pass 5: recovery and analytics ------------------------------------

    async def resync(self) -> None:
        """Recover labeled issues that never reached the inbox."""
        try:
            issues = await self.github.list_issues_with_label(self.settings.trigger_label)
        except Exception:
            logger.exception("resync failed")
            return

        known = {i["number"] for i in self.repo.issues()}
        for issue in issues:
            if issue["number"] in known:
                continue
            logger.warning("resync recovered issue #%s (no webhook was processed)", issue["number"])
            self.repo.bump("resync_recovered")
            try:
                await self._register_issue(issue["number"])
            except Exception:
                logger.exception("resync could not register #%s", issue["number"])

    async def refresh_insights(self) -> None:
        """Reconcile against Devin's Analytics endpoint.

        The tag filter is sent but not trusted to have been applied: the endpoint accepts unknown
        query parameters silently, so a renamed filter would return the whole organization rather
        than an error. Rows are matched against session ids this orchestrator created.

        One page. At this scale there are a handful of sessions, and a truncation is visible as a
        gap between `insights_applied` and the session count rather than silently wrong.
        """
        try:
            response = await self.devin.insights(tags=[ORCHESTRATOR_TAG], first=200)
        except Exception:
            logger.exception("insights refresh failed")
            self.repo.bump("insights_errors")
            return

        applied = foreign = 0
        for row in collection_items(response):
            session_id = row.get("session_id")
            if not session_id:
                continue
            if self.repo.apply_insight(
                session_id,
                acus=float(row.get("acus_consumed") or 0),
                devin_messages=row.get("num_devin_messages"),
                user_messages=row.get("num_user_messages"),
                session_size=row.get("session_size"),
            ):
                applied += 1
            else:
                foreign += 1

        self.repo.bump("insights_applied", applied)
        if foreign:
            # Non-zero means the tag filter is not doing what the request asked for.
            self.repo.bump("insights_rows_not_ours", foreign)

    # -- the view the dashboard and the metrics share -----------------------

    def issue_view(self) -> list[dict[str, Any]]:
        rows = []
        for ctx in self.contexts():
            sessions, pulls = ctx["sessions"], ctx["pulls"]
            session = sessions[-1] if sessions else None
            merged = next((p for p in pulls if p["merged_at"]), None)
            rows.append(
                {
                    **ctx["issue"],
                    "status": ctx["status"],
                    "session": session,
                    "pull_request": merged or (pulls[-1] if pulls else None),
                    "structured_output": json.loads(session["structured_output"])
                    if session and session["structured_output"]
                    else None,
                }
            )
        return rows


__all__ = ["ORCHESTRATOR_TAG", "Orchestrator", "parse_pr_number"]
