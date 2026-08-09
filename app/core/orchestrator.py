"""The reconcile loop.

Derives status from facts, decides what should change, and asks :mod:`app.core.effects` to change
it. It performs no writes of its own — no API client is called from this module — which is what
keeps the concurrency, ACU and message guards enforceable in one place.

Five passes run at different cadences inside a single tick:

======================  ========  ==========================================================
Pass                    Cadence   Work
======================  ========  ==========================================================
drain inbox             every     Act on intent the receiver recorded
start sessions          every     Issues whose derived status is ``queued``
track sessions          every     Poll, advance, nudge, notify, report
track pull requests     60s       CI outcome and merge state
resync + analytics      300s      Recover lost events; reconcile ACUs and message counts
======================  ========  ==========================================================

Ticks are serialised. The admin endpoint runs ``tick()`` on the same event loop as the background
task, and without the lock two ticks interleave at the ``await`` inside session creation — two paid
sessions for one issue, and the concurrency cap bypassed.

Resync is not redundant with webhooks: GitHub does not retry failed deliveries, so an event arriving
while this process is down is gone for good.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from app.clients.devin import collection_items, last_devin_message
from app.config import Settings
from app.core import prompts
from app.core.effects import AmbiguousEffect, Effects, Reason
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
    wants_session,
)
from app.db.repo import Repo, now

logger = logging.getLogger(__name__)

_PR_URL = re.compile(r"/pull/(\d+)$")

#: Tag on every session this orchestrator creates. Reviewers cross-check the tags visible in the
#: Devin dashboard against what the orchestrator claims to send, so these are real identifiers only.
ORCHESTRATOR_TAG = "orchestrator:superset-remediation"

MAX_INBOX_ATTEMPTS = 3

#: Inbox kinds carrying information nothing else reconstructs. Giving up on one is worth saying.
IRRECOVERABLE_KINDS = frozenset({"issue_comment", "review_comment"})


def parse_pr_number(url: str | None, repo: str) -> int | None:
    """Extract a pull request number, but only for our own repository.

    Anchored on purpose: a loose ``/pull/(\\d+)`` search accepted a URL for any repository and the
    orchestrator then acted on that number against ours.
    """
    if not url:
        return None
    prefix = f"https://github.com/{repo}"
    if not url.startswith(prefix):
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

    # -- pass 1: recorded intent -------------------------------------------

    async def drain_inbox(self) -> None:
        for item in self.repo.pending_inbox():
            try:
                await self._handle(item["kind"], item["payload"])
            except AmbiguousEffect as exc:
                # The effect may already have happened, so retrying is not safe. Stop and say so.
                logger.warning("inbox item %s (%s) is ambiguous: %s", item["id"], item["kind"], exc)
                self.repo.bump("inbox_errors")
                self.repo.bump("inbox_abandoned")
                self.repo.mark_dispatched(item["id"])
                await self._report_abandoned(item, exc)
            except Exception as exc:
                logger.exception("inbox item %s (%s) failed", item["id"], item["kind"])
                self.repo.bump("inbox_errors")
                if self.repo.record_inbox_failure(
                    item["id"], repr(exc), max_attempts=MAX_INBOX_ATTEMPTS
                ):
                    self.repo.bump("inbox_abandoned")
                    await self._report_abandoned(item, exc)
            else:
                self.repo.mark_dispatched(item["id"])

    async def _handle(self, kind: str, payload: dict[str, Any]) -> None:
        match kind:
            case "issue_labeled":
                await self._register_issue(payload["number"])
            case "ci_failed":
                await self._feed_ci_failure(payload["pr_number"], payload.get("sha"))
            case "review_comment":
                await self._forward_comment(
                    self.repo.pull_request(payload["pr_number"]),
                    author=payload["author"],
                    comment=payload["comment"],
                    comment_id=payload["comment_id"],
                    is_review=True,
                )
            case "issue_comment":
                await self._forward_comment(
                    {"issue_number": payload["issue_number"]},
                    author=payload["author"],
                    comment=payload["comment"],
                    comment_id=payload["comment_id"],
                    is_review=False,
                )
            case "pr_closed":
                record = self.repo.pull_request(payload["pr_number"])
                if record:
                    await self._advance_pull_request(record)
            case _:
                raise ValueError(f"no handler for inbox kind: {kind}")

    async def _report_abandoned(self, item: dict[str, Any], exc: Exception) -> None:
        if item["kind"] not in IRRECOVERABLE_KINDS:
            return
        issue_number = item["payload"].get("issue_number")
        if issue_number is None:
            pr = self.repo.pull_request(item["payload"].get("pr_number", -1))
            issue_number = pr["issue_number"] if pr else None
        if issue_number is None:
            return
        await self.effects.notify(
            issue_number,
            reason_class=Reason.UNDELIVERABLE,
            detail=(
                f"A `{item['kind']}` could not be delivered to the Devin session after "
                f"{MAX_INBOX_ATTEMPTS} attempts (`{exc!r}`). Nothing else reconstructs it, so "
                "please repeat it once the orchestrator is healthy."
            ),
        )

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

        # Already known: re-applying the label means "try again". That is one fact, and the derived
        # status picks it up on the next pass.
        self.repo.request_retry(number)
        self.repo.bump("retries_requested")
        await self.effects.resolve(number)
        logger.info("retry requested for issue #%s", number)

    # -- pass 2: start sessions --------------------------------------------

    async def start_sessions(self) -> None:
        deferred_for_budget: int | None = None
        for issue in self.repo.issues():
            sessions = self.repo.sessions(issue["number"])
            if not wants_session(issue, sessions):
                continue

            active = sum(1 for s in self.repo.sessions() if occupies_slot(s))
            if active >= self.settings.max_concurrent_sessions:
                self.repo.bump("start_deferred_concurrency")
                return
            spent = self.repo.total_acus()
            if spent >= self.settings.global_acu_budget:
                deferred_for_budget = issue["number"]
                break

            try:
                await self._create_session(issue, attempt=len(sessions) + 1)
            except Exception:
                logger.exception("starting a session for #%s failed", issue["number"])
                self.repo.bump("session_start_errors")
                await self.effects.notify(
                    issue["number"],
                    reason_class=Reason.START_FAILED,
                    detail=(
                        "Creating a Devin session failed. A session may or may not have been "
                        "created, so this issue is held rather than retried automatically — that "
                        "cannot bill twice. Check the Devin dashboard, then re-apply the label."
                    ),
                )

        if deferred_for_budget is not None:
            await self.effects.notify(
                deferred_for_budget,
                reason_class=Reason.BUDGET_EXHAUSTED,
                detail=(
                    f"The global ACU budget ({self.settings.global_acu_budget}) is spent, so no "
                    "further sessions will start. Raise `GLOBAL_ACU_BUDGET` to continue."
                ),
            )

    async def _create_session(self, issue: dict[str, Any], *, attempt: int) -> None:
        number = issue["number"]
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
            key=f"issue:{number}:started:{session['session_id']}",
        )

    # -- pass 3: track sessions --------------------------------------------

    async def track_sessions(self) -> None:
        max_age = self.settings.max_session_age_hours * 3600
        for record in self.repo.sessions():
            if liveness(record) is Liveness.CLOSED:
                continue
            # The backstop for anything the status vocabulary does not express: an unrecognised
            # status, or a session that rests indefinitely having produced nothing. Age alone, not
            # age-and-unfinished — v1's extra condition exempted the runaway case and killed the
            # healthy one.
            if now() - record["created_at"] > max_age:
                self.repo.close_session(record["session_id"], "timeout")
                self.repo.bump("sessions_timed_out")
                await self.effects.notify(
                    record["issue_number"],
                    reason_class=Reason.SESSION_TIMEOUT,
                    detail=(
                        f"The session has been open for over "
                        f"{self.settings.max_session_age_hours:.0f}h and is no longer tracked."
                    ),
                    session=record,
                )
                continue
            try:
                await self._advance_session(record)
            except Exception:
                logger.exception("advancing session %s failed", record["session_id"])

    async def _advance_session(self, record: dict[str, Any]) -> None:
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

        if produced:
            await self._report_completion(current, structured, pulls)

        if reason and reason.startswith("cost_halt"):
            await self.effects.notify(
                record["issue_number"],
                reason_class=Reason.COST_HALT,
                detail=f"Devin stopped: `{detail}`. No message can revive this session.",
                session=current,
            )
        elif reason == "error":
            await self.effects.notify(
                record["issue_number"],
                reason_class=Reason.SESSION_ERROR,
                detail="The Devin session ended in an error. Re-apply the label to try again.",
                session=current,
            )
        elif reason == "exit" and not produced:
            await self.effects.notify(
                record["issue_number"],
                reason_class=Reason.SESSION_ERROR,
                detail=(
                    "The Devin session ended without producing a pull request or a final result. "
                    "Re-apply the label to try again."
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
        if session["nudges"] < self.settings.max_nudges:
            sent = await self.effects.message_session(
                session,
                reason="auto_nudge",
                body=prompts.nudge_message(),
                key=f"session:{session['session_id']}:nudge:{session['nudges'] + 1}",
                respect_grace=True,
            )
            if sent:
                self.repo.bump_nudges(session["session_id"])
            return

        question = ""
        try:
            question = last_devin_message(await self.devin.list_messages(session["session_id"]))
        except Exception:
            logger.debug("could not read messages for %s", session["session_id"], exc_info=True)
        await self.effects.notify(
            session["issue_number"],
            reason_class=Reason.BLOCKED_ON_QUESTION,
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
                    await self.effects.resolve(record["issue_number"])
                logger.info("PR #%s merged", number)
            return
        if pull.get("state") == "closed":
            if record["closed_at"] is None:
                self.repo.update_pr(number, closed_at=now())
                self.repo.bump("prs_closed_unmerged")
                if record["issue_number"]:
                    await self.effects.notify(
                        record["issue_number"],
                        reason_class=Reason.PR_CLOSED_UNMERGED,
                        detail=(
                            f"{record['url']} was closed without merging, so this issue is not "
                            "fixed. Re-apply the label to try again."
                        ),
                    )
            return

        sha = pull["head"]["sha"]
        settled, conclusion = await self.github.checks_settled(sha)
        if settled and not record["ci_settled_at"]:
            self.repo.update_pr(number, ci_settled_at=now(), ci_conclusion=conclusion)
        if settled and conclusion == "failure":
            await self._feed_ci_failure(number, sha)

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

        if record["ci_rounds"] >= self.settings.max_ci_feedback_rounds:
            await self.effects.notify(
                record["issue_number"],
                reason_class=Reason.CI_UNRESOLVED,
                detail=(
                    f"CI is still failing on {record['url']} after "
                    f"{self.settings.max_ci_feedback_rounds} self-correction attempts. Note that "
                    "some checks on this fork are flaky or cannot run at all — see the CI baseline "
                    "in the README before assuming the code is at fault."
                ),
                session=session,
            )
            return

        failed = await self.github.failed_check_summary(sha)
        if not failed:
            # Settled-as-failure with nothing named means the two reads disagreed transiently. The
            # ledger key is never claimed here, so the retry fires once the names appear.
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
            self.repo.update_pr(pr_number, ci_rounds=rounds)
            logger.info("handed CI failure back for PR #%s (round %s)", pr_number, rounds)

    async def _forward_comment(
        self,
        target: dict[str, Any] | None,
        *,
        author: str,
        comment: str,
        comment_id: int,
        is_review: bool,
    ) -> None:
        """Forward trusted human text to the session working on this issue.

        Keyed on the comment id, so a second reply is delivered rather than dropped and delivery
        does not depend on the issue being in a particular state. v1 gated on an issue-state check
        that another path reset, and silently lost every reply after the first.
        """
        if not target or target.get("issue_number") is None:
            return
        issue_number = target["issue_number"]
        session = self.repo.latest_session_for_issue(issue_number)
        if not session:
            return

        pr = self.repo.pull_request(target["pr_number"]) if is_review else None
        body = (
            prompts.review_feedback_message(
                pr_url=(pr or {}).get("url") or "", reviewer=author, comment=comment
            )
            if is_review
            else prompts.human_reply_message(author=author, comment=comment)
        )

        if await self.effects.message_session(
            session,
            reason="review_feedback" if is_review else "human_reply",
            body=body,
            key=f"comment:{comment_id}",
            issue_number=issue_number,
        ):
            # A human took over and handed back, so the automatic budgets start again. Without this
            # the next poll re-enters the exhausted branch and re-raises what was just answered.
            self.repo.reset_budgets(session["session_id"])
            await self.effects.resolve(issue_number)

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
        """Reconcile against Devin's Analytics endpoint, following pagination.

        The tag filter is sent but not trusted to have been applied: the endpoint accepts unknown
        query parameters silently, so a renamed filter would return the whole organization rather
        than an error and other people's sessions would enter the cost figures. Rows are matched
        against session ids this orchestrator created.
        """
        applied = foreign = 0
        cursor: str | None = None
        try:
            for _ in range(20):
                response = await self.devin.insights(
                    tags=[ORCHESTRATOR_TAG], first=200, after=cursor
                )
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
                if not isinstance(response, dict) or not response.get("has_next_page"):
                    break
                cursor = response.get("end_cursor")
                if not cursor:
                    break
        except Exception:
            logger.exception("insights refresh failed")
            self.repo.bump("insights_errors")
            return

        self.repo.bump("insights_applied", applied)
        if foreign:
            # A non-zero value means the tag filter is not doing what the request asked for.
            self.repo.bump("insights_rows_not_ours", foreign)

    # -- the view the dashboard and the metrics share -----------------------

    def issue_view(self) -> list[dict[str, Any]]:
        rows = []
        for issue in self.repo.issues():
            sessions = self.repo.sessions(issue["number"])
            pulls = self.repo.pull_requests(issue["number"])
            notifications = self.repo.open_notifications(issue["number"])
            session = sessions[-1] if sessions else None
            merged = next((p for p in pulls if p["merged_at"]), None)
            rows.append(
                {
                    **issue,
                    "status": issue_status(issue, sessions, pulls, notifications),
                    "session": session,
                    "pull_request": merged or (pulls[-1] if pulls else None),
                    "notifications": notifications,
                    "attempts": len(sessions),
                    "structured_output": json.loads(session["structured_output"])
                    if session and session["structured_output"]
                    else None,
                }
            )
        return rows

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.issue_view():
            key = str(row["status"])
            counts[key] = counts.get(key, 0) + 1
        return counts


__all__ = ["ORCHESTRATOR_TAG", "IssueStatus", "Orchestrator", "parse_pr_number"]
