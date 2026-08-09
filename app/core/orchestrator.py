"""The reconcile loop.

Everything with a side effect happens here. The webhook receiver only records intent, which is what
makes the concurrency, ACU, and nudge limits enforceable in one place instead of being re-argued at
every call site.

The loop compares desired state ("this issue is labeled, so it should end up fixed") against actual
state (session phase, pull request state) and closes the gap. Five passes run at different cadences
inside a single tick:

======================  ========  ==========================================================
Pass                    Cadence   Work
======================  ========  ==========================================================
drain queue             every     Act on intent recorded by the webhook receiver
start sessions          every     Create sessions for issues that have none
track sessions          every     Poll Devin, advance state, nudge, escalate, write back
track pull requests     60s       CI outcome and merge state
resync                  300s      Recover events lost while the service was down
======================  ========  ==========================================================

The resync pass is not redundant with webhooks. GitHub does not retry failed deliveries, so an event
that arrives while this process is down is gone for good; without resync, an issue labeled during a
restart would never be picked up.

Invariants worth knowing before editing:

* **Ticks are serialised.** `tick()` holds a lock, because the admin endpoint runs it on the same
  event loop as the background loop. Without it, two ticks interleave at the `await` inside session
  creation and both see the same issue as unstarted — two paid sessions, and the concurrency cap
  bypassed.
* **The issue is reserved before Devin is called.** Any failure after a billable call must never
  lead to another billable call for the same issue.
* **`finished_at` and `closed_at` are different things.** `finished_at` means the session produced
  its work product; `closed_at` means it can no longer be revived at all. Polling and the feedback
  paths key off `closed_at`, because a session that opened a pull request and went to sleep is
  finished but still wakeable — which is exactly what the review-fix loop needs.
* **Nothing is nudged inside the grace period.** After any outbound message a session needs a
  moment to act on it; without this, forwarding a human's answer is followed microseconds later by
  another escalation, because the session still reads `waiting_for_user`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from app.clients.devin import DevinClient, collection_items, last_devin_message
from app.clients.github import GitHubClient
from app.clients.http import ApiError
from app.config import Settings
from app.core import prompts
from app.core.policy import (
    Envelope,
    can_start_session,
    is_startable,
    nudge_or_escalate,
    should_send_ci_feedback,
)
from app.core.state import (
    ACTIVE_PHASES,
    CLOSED_PHASES,
    IssueState,
    Phase,
    classify,
    is_work_done,
    issue_state_for,
)
from app.db.repo import Repo, now

logger = logging.getLogger(__name__)

_PR_URL = re.compile(r"/pull/(\d+)")

#: Tag applied to every session this orchestrator creates. Reviewers cross-check the tags visible
#: in the Devin dashboard against what the orchestrator claims to send, so these are built from
#: real identifiers only.
ORCHESTRATOR_TAG = "orchestrator:superset-remediation"

#: Queue items are retried this many times before being abandoned. `issue_comment` and
#: `review_comment` have no other source — no polling pass re-derives them — so dropping one on a
#: transient API error loses a human's answer permanently.
MAX_QUEUE_ATTEMPTS = 3

#: Queue kinds that carry information nothing else can reconstruct. Exhausting the retries on one
#: of these is worth telling a human about.
IRRECOVERABLE_KINDS = frozenset({"issue_comment", "review_comment"})


def parse_pr_number(url: str | None) -> int | None:
    if not url:
        return None
    match = _PR_URL.search(url)
    return int(match.group(1)) if match else None


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        repo: Repo,
        devin: DevinClient,
        github: GitHubClient,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.devin = devin
        self.github = github
        self.envelope = Envelope(
            max_concurrent_sessions=settings.max_concurrent_sessions,
            max_acu_per_session=settings.max_acu_per_session,
            global_acu_budget=settings.global_acu_budget,
            max_nudges=settings.max_nudges,
            max_ci_feedback_rounds=settings.max_ci_feedback_rounds,
        )
        self._tick = 0
        self._playbook_id = settings.devin_playbook_id
        self._lock = asyncio.Lock()

    # -- lifecycle ----------------------------------------------------------

    async def run_forever(self) -> None:
        interval = self.settings.session_poll_interval
        pr_every = max(1, round(self.settings.pr_poll_interval / interval))
        resync_every = max(1, round(self.settings.resync_interval / interval))

        while True:
            try:
                await self.tick(pr_every=pr_every, resync_every=resync_every)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failing pass must not kill the loop; the next tick re-derives everything from
                # the database anyway.
                logger.exception("reconcile tick failed")
                self.repo.bump("tick_errors")
            await asyncio.sleep(interval)

    async def tick(self, *, pr_every: int = 6, resync_every: int = 30) -> None:
        """One reconcile pass. Serialised: concurrent callers queue rather than interleave."""
        async with self._lock:
            self._tick += 1
            await self.drain_queue()
            await self.start_sessions()
            await self.track_sessions()
            if self._tick % pr_every == 0:
                await self.track_pull_requests()
            # `% n == 0` rather than `== 1`: with n == 1 the latter is never true, which silently
            # disabled resync exactly when it was asked for most often (the admin endpoint).
            if self._tick % resync_every == 0:
                await self.resync()
                await self.refresh_insights()

    # -- pass 1: act on recorded intent ------------------------------------

    async def drain_queue(self) -> None:
        for item in self.repo.pending_queue():
            try:
                await self._handle_queued(item["kind"], item["payload"])
            except Exception as exc:
                logger.exception("queue item %s (%s) failed", item["id"], item["kind"])
                self.repo.bump("queue_errors")
                exhausted = self.repo.record_queue_failure(
                    item["id"], repr(exc), max_attempts=MAX_QUEUE_ATTEMPTS
                )
                if exhausted:
                    self.repo.bump("queue_abandoned")
                    await self._report_abandoned_item(item, exc)
            else:
                self.repo.mark_dispatched(item["id"])

    async def _report_abandoned_item(self, item: dict[str, Any], exc: Exception) -> None:
        """Tell a human when intent nothing else can rebuild has been given up on."""
        if item["kind"] not in IRRECOVERABLE_KINDS:
            return
        issue_number = item["payload"].get("issue_number")
        if issue_number is None:
            pr = self.repo.pull_request(item["payload"].get("pr_number", -1))
            issue_number = pr["issue_number"] if pr else None
        if issue_number is None:
            return
        self.repo.record_intervention(
            "dropped_message",
            session_id=None,
            issue_number=issue_number,
            detail=f"{item['kind']}: {exc!r}",
        )
        try:
            await self.github.comment(
                issue_number,
                "⚠️ A comment on this issue could not be delivered to the Devin session after "
                f"{MAX_QUEUE_ATTEMPTS} attempts (`{item['kind']}`). Nothing else reconstructs it, "
                "so please repeat it once the orchestrator is healthy.",
            )
        except Exception:
            logger.exception("could not report an abandoned queue item on #%s", issue_number)

    async def _handle_queued(self, kind: str, payload: dict[str, Any]) -> None:
        match kind:
            case "issue_labeled":
                await self._register_issue(payload["number"])
            case "ci_failed":
                await self._feed_ci_failure(payload["pr_number"], payload.get("sha"))
            case "review_comment":
                await self._feed_review_comment(
                    payload["pr_number"], payload["reviewer"], payload["comment"]
                )
            case "issue_comment":
                await self._feed_human_reply(
                    payload["issue_number"], payload["author"], payload["comment"]
                )
            case "pr_closed":
                record = self.repo.pull_request(payload["pr_number"])
                if record:
                    await self._advance_pull_request(record)
            case _:
                logger.warning("unknown queue kind: %s", kind)

    async def _register_issue(self, number: int) -> None:
        """Trust-but-verify: confirm the label is really on the issue before spending anything."""
        issue = await self.github.get_issue(number)
        labels = {label["name"] for label in issue.get("labels", [])}
        if self.settings.trigger_label not in labels:
            logger.info("#%s no longer carries %s; ignoring", number, self.settings.trigger_label)
            self.repo.bump("stale_events_ignored")
            return
        if issue.get("state") != "open":
            logger.info("#%s is closed; ignoring", number)
            self.repo.bump("stale_events_ignored")
            return

        klass = next((label for label in labels if label.startswith("class:")), None)
        if self.repo.upsert_issue(number, issue.get("title", ""), klass, now()):
            logger.info("registered issue #%s (%s)", number, klass)
            return

        # Already known. Re-applying the label is the operator's way of saying "try again", so a
        # stalled issue is returned to the queue. Without this there is no path back from `failed`
        # or `escalated` at all — `upsert_issue` is INSERT OR IGNORE, so re-labelling did nothing.
        if self.repo.reopen_issue(number):
            logger.info("re-labelled issue #%s returned to pending", number)
            self.repo.bump("issues_reopened")
        else:
            self.repo.bump("duplicate_issue_events")

    # -- pass 2: start sessions --------------------------------------------

    async def start_sessions(self) -> None:
        pending = [i for i in self.repo.issues() if i["state"] == IssueState.PENDING]
        if not pending:
            return

        for issue in pending:
            existing = self.repo.latest_session_for_issue(issue["number"])
            phase = classify(existing["status"], existing["status_detail"]) if existing else None
            if existing is not None and not is_startable(phase):
                continue

            allowed, reason = can_start_session(
                self.envelope,
                active_sessions=self._active_session_count(),
                acus_spent=self.repo.total_acus(),
            )
            if not allowed:
                logger.info("holding issue #%s: %s", issue["number"], reason)
                self.repo.bump("start_deferred")
                return

            # Per-issue guard: one failure must not abort the whole tick and skip session and
            # pull-request tracking for this cycle.
            try:
                await self._create_session(issue)
            except Exception:
                logger.exception("starting a session for #%s failed", issue["number"])
                self.repo.bump("session_start_errors")

    def _active_session_count(self) -> int:
        return sum(
            1
            for s in self.repo.sessions()
            if s["closed_at"] is None and classify(s["status"], s["status_detail"]) in ACTIVE_PHASES
        )

    async def _create_session(self, issue: dict[str, Any]) -> None:
        number = issue["number"]
        issue_url = f"https://github.com/{self.settings.github_repo}/issues/{number}"
        tags = [ORCHESTRATOR_TAG, f"repo:{self.settings.github_repo}", f"issue:{number}"]
        if issue.get("klass"):
            tags.append(issue["klass"])

        # Reserve before spending. If anything below fails, the issue is no longer `pending`, so no
        # later tick can start a second billable session for it.
        self.repo.set_issue_state(number, IssueState.RUNNING)

        try:
            response = await self.devin.create_session(
                prompts.session_prompt(
                    repo=self.settings.github_repo,
                    issue_number=number,
                    issue_title=issue.get("title", ""),
                    issue_url=issue_url,
                ),
                title=f"superset#{number}: {issue.get('title', '')}"[:120],
                tags=tags,
                repo=self.settings.github_repo,
                max_acu_limit=self.settings.max_acu_per_session,
                playbook_id=self._playbook_id,
            )
        except ApiError as exc:
            if exc.status < 500:
                # A 4xx means the request was rejected: nothing was created and nothing was billed,
                # so the issue is safe to return to the queue.
                self.repo.set_issue_state(number, IssueState.PENDING, force=True)
                self.repo.bump("session_start_rejected")
                raise
            await self._escalate_issue(
                number,
                None,
                f"Devin returned `{exc.status}` while creating a session. A session may or may not "
                "have been created; this issue is held rather than retried so it cannot be billed "
                "twice. Check the Devin dashboard and re-apply the label if nothing is running.",
            )
            raise
        except Exception:
            await self._escalate_issue(
                number,
                None,
                "The call to create a Devin session did not complete. A session may or may not "
                "have been created; this issue is held rather than retried so it cannot be billed "
                "twice. Check the Devin dashboard and re-apply the label if nothing is running.",
            )
            raise

        session_id = response.get("session_id") if isinstance(response, dict) else None
        if not session_id:
            await self._escalate_issue(
                number,
                None,
                "Devin accepted the session request but returned no `session_id`, so the session "
                "cannot be tracked. Check the Devin dashboard before re-applying the label.",
            )
            raise RuntimeError(f"create_session returned no session_id for #{number}")

        self.repo.create_session(session_id, number, response.get("url"), tags)
        self.repo.bump("sessions_created")
        logger.info("session %s started for issue #%s", session_id, number)

        # The session exists and is recorded; a failure to comment must not undo that.
        try:
            await self.github.comment(
                number,
                "🤖 Devin session started for this issue.\n\n"
                f"- Session: {response.get('url')}\n"
                f"- Tags: `{'`, `'.join(tags)}`\n"
                f"- ACU cap: {self.settings.max_acu_per_session}\n\n"
                "Progress will be reported here.",
            )
        except Exception:
            logger.exception("could not comment the session start on #%s", number)
            self.repo.bump("comment_errors")

    # -- pass 3: track sessions --------------------------------------------

    async def track_sessions(self) -> None:
        max_age = self.settings.max_session_age_hours * 3600
        for record in self.repo.sessions():
            if record["closed_at"] is not None:
                continue
            # Backstop for anything the phase vocabulary does not cover: an unrecognised status, or
            # a session that sleeps indefinitely having produced nothing. Both would otherwise hold
            # a concurrency slot and be polled forever.
            if now() - record["created_at"] > max_age and record["finished_at"] is None:
                self.repo.close_session(record["session_id"])
                self.repo.bump("sessions_timed_out")
                await self._escalate_issue(
                    record["issue_number"],
                    record,
                    "The session has been open for over "
                    f"{self.settings.max_session_age_hours:.0f}h "
                    "without producing a pull request or structured output, so it is no longer "
                    "being tracked.",
                )
                continue
            try:
                await self._advance_session(record)
            except Exception:
                logger.exception("advancing session %s failed", record["session_id"])

    def _in_grace(self, record: dict[str, Any]) -> bool:
        """Whether the session was recently sent something and deserves time to act on it."""
        last = record.get("last_message_at")
        return last is not None and (now() - last) < self.settings.message_grace_seconds

    async def _advance_session(self, record: dict[str, Any]) -> None:
        session_id = record["session_id"]
        remote = await self.devin.get_session(session_id)

        status = remote.get("status")
        detail = remote.get("status_detail")
        phase = classify(status, detail)
        structured = remote.get("structured_output")
        pulls = remote.get("pull_requests") or []
        done = is_work_done(
            phase, has_structured_output=bool(structured), has_pull_request=bool(pulls)
        )
        closed = phase in CLOSED_PHASES

        changed = self.repo.update_session(
            session_id,
            status=status,
            status_detail=detail,
            acus=float(remote.get("acus_consumed") or 0),
            structured_output=structured,
            blocked=phase is Phase.BLOCKED,
            finished=done,
            # Polling stops on `closed`, not on `finished`. A session that opened a pull request and
            # went to sleep has finished its work but is still wakeable, and the review-fix loop
            # depends on being able to message it.
            closed=closed,
        )
        if changed:
            logger.info("session %s -> %s/%s", session_id, status, detail)

        if pulls:
            self._link_pull_requests(record, pulls)

        # Completion is checked before blocked: a session can report structured output while still
        # showing `waiting_for_user`, and reporting the result matters more than nudging it.
        if done:
            await self._report_completion(record, structured, pulls)
        elif phase is Phase.FAILED:
            self.repo.set_issue_state(record["issue_number"], IssueState.FAILED)
            self.repo.bump("sessions_failed")
            await self._escalate_issue(
                record["issue_number"],
                record,
                f"The Devin session ended in `{status}`"
                + (f"/`{detail}`" if detail else "")
                + ". Re-apply the label to try again.",
            )
        elif phase is Phase.HALTED_COST:
            await self._escalate_issue(
                record["issue_number"],
                record,
                f"Devin stopped: `{detail}`. A cost or quota ceiling was reached, so the session "
                "cannot be resumed by sending it a message.",
            )
        elif phase is Phase.BLOCKED and not self._in_grace(record):
            await self._handle_blocked(record, session_id)

    def _link_pull_requests(self, record: dict[str, Any], pulls: list[dict[str, Any]]) -> None:
        for pull in pulls:
            number = parse_pr_number(pull.get("pr_url"))
            if number is None:
                continue
            created = self.repo.upsert_pr(
                number,
                issue_number=record["issue_number"],
                session_id=record["session_id"],
                url=pull.get("pr_url"),
                opened_at=now(),
                state=pull.get("pr_state") or "open",
            )
            if created:
                self.repo.set_issue_state(record["issue_number"], IssueState.PR_OPEN)
                self.repo.bump("prs_opened")

    async def _handle_blocked(self, record: dict[str, Any], session_id: str) -> None:
        decision = nudge_or_escalate(self.envelope, nudges_sent=record["nudges"])
        if decision == "nudge":
            await self.devin.send_message(session_id, prompts.nudge_message())
            self.repo.mark_message_sent(session_id)
            self.repo.bump_session(session_id, "nudges")
            self.repo.record_intervention(
                "auto_nudge",
                session_id=session_id,
                issue_number=record["issue_number"],
                detail="session reported waiting_for_user",
            )
            self.repo.bump("nudges_sent")
            logger.info("nudged session %s", session_id)
            return

        question = ""
        try:
            question = last_devin_message(await self.devin.list_messages(session_id))
        except Exception:
            logger.debug("could not read messages for %s", session_id, exc_info=True)
        await self._escalate_issue(
            record["issue_number"],
            record,
            "Devin is blocked on a question and the automatic nudge limit "
            f"({self.envelope.max_nudges}) is exhausted."
            + (f"\n\n> {question}" if question else ""),
        )

    async def _escalate_issue(
        self, number: int, record: dict[str, Any] | None, reason: str
    ) -> None:
        issue = self.repo.issue(number)
        if issue and issue["state"] == IssueState.ESCALATED:
            return
        if not self.repo.set_issue_state(number, IssueState.ESCALATED):
            # Refused because the issue already reached a sticky outcome (merged). Nothing to do.
            return

        self.repo.record_intervention(
            "escalation",
            session_id=record["session_id"] if record else None,
            issue_number=number,
            detail=reason,
        )
        self.repo.bump("escalations")
        session_line = f"\n\nSession: {record['url']}" if record and record.get("url") else ""
        try:
            await self.github.add_label(number, self.settings.escalation_label)
            await self.github.comment(
                number,
                f"🙋 **Human input needed**\n\n{reason}{session_line}\n\n"
                "Reply on this issue and the answer will be forwarded to the session, which will "
                "resume from where it stopped.",
            )
        except Exception:
            logger.exception("could not write the escalation on #%s", number)
            self.repo.bump("comment_errors")
        logger.info("escalated issue #%s", number)

    async def _report_completion(
        self, record: dict[str, Any], structured: Any, pulls: list[dict[str, Any]]
    ) -> None:
        session_id = record["session_id"]
        # Claim the report atomically. Kept separate from `finished_at` because overloading one
        # column meant a session that finished while blocked latched it on the nudge path and could
        # then never report at all.
        if not self.repo.mark_reported(session_id):
            return

        number = record["issue_number"]
        data = structured if isinstance(structured, dict) else {}
        outcome = data.get("outcome", "unknown")
        tests = data.get("tests_added") or []
        assumptions = data.get("assumptions") or []
        follow_up = data.get("follow_up") or ""
        # Re-read so the reported spend is the freshly polled figure, not the pre-poll row.
        acus = (self.repo.session(session_id) or record)["acus"]

        lines = [f"✅ **Devin finished** — outcome: `{outcome}`", ""]
        if data.get("root_cause"):
            lines += [f"**Root cause.** {data['root_cause']}", ""]
        if data.get("summary"):
            lines += [data["summary"], ""]
        for pull in pulls:
            lines.append(f"- Pull request: {pull.get('pr_url')} (`{pull.get('pr_state')}`)")
        if tests:
            lines += ["", "**Tests added.**"] + [f"- `{t}`" for t in tests]
        if assumptions:
            lines += ["", "**Assumptions.**"] + [f"- {a}" for a in assumptions]
        if follow_up:
            lines += ["", f"**Left undone.** {follow_up}"]
        lines += ["", f"ACUs consumed: {acus:.2f} · Session: {record['url']}"]

        try:
            await self.github.comment(number, "\n".join(lines))
        except Exception:
            logger.exception("could not comment the completion on #%s", number)
            self.repo.bump("comment_errors")
        self.repo.bump("completions_reported")
        logger.info("reported completion for issue #%s", number)

    # -- pass 4: pull request outcomes -------------------------------------

    async def track_pull_requests(self) -> None:
        for record in self.repo.open_pull_requests():
            try:
                await self._advance_pull_request(record)
            except Exception:
                logger.exception("advancing PR #%s failed", record["pr_number"])

    async def _advance_pull_request(self, record: dict[str, Any]) -> None:
        number = record["pr_number"]
        pull = await self.github.get_pull(number)

        if pull.get("merged_at"):
            self.repo.update_pr(number, state="merged", merged_at=now())
            if record["issue_number"]:
                self.repo.set_issue_state(record["issue_number"], IssueState.MERGED, force=True)
            self.repo.bump("prs_merged")
            logger.info("PR #%s merged", number)
            return
        if pull.get("state") == "closed":
            self.repo.update_pr(number, state="closed", closed_at=now())
            self.repo.bump("prs_closed_unmerged")
            return

        sha = pull["head"]["sha"]
        settled, conclusion = await self.github.checks_settled(sha)
        if settled and not record["ci_settled_at"]:
            self.repo.update_pr(number, ci_settled_at=now(), ci_conclusion=conclusion)
        if settled and conclusion == "failure":
            await self._feed_ci_failure(number, sha)

    # -- review-fix loop ----------------------------------------------------

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

        # One round per commit. Without this the whole feedback budget is spent re-reporting the
        # same red commit on consecutive polls, minutes apart, before Devin can push anything.
        if record["ci_feedback_sha"] == sha:
            self.repo.bump("ci_feedback_deduped")
            return

        if not should_send_ci_feedback(self.envelope, rounds_used=session["ci_rounds"]):
            await self._escalate_issue(
                record["issue_number"],
                session,
                f"CI is still failing on {record['url']} after "
                f"{self.envelope.max_ci_feedback_rounds} self-correction attempts.",
            )
            return

        failed = await self.github.failed_check_summary(sha)
        if not failed:
            # Settled-as-failure with nothing named means the two conclusion sets disagree. They
            # share one definition now, so this should be unreachable; log it rather than looping.
            logger.warning("PR #%s reported failing with no named check on %s", pr_number, sha)
            self.repo.bump("ci_failure_without_detail")
            self.repo.update_pr(pr_number, ci_feedback_sha=sha)
            return

        rounds = session["ci_rounds"] + 1
        await self.devin.send_message(
            session["session_id"],
            prompts.ci_failure_message(
                pr_url=record["url"] or "", failed_checks=failed, round_number=rounds
            ),
        )
        self.repo.mark_message_sent(session["session_id"])
        self.repo.bump_session(session["session_id"], "ci_rounds")
        self.repo.update_pr(pr_number, ci_attempts=rounds, ci_feedback_sha=sha)
        self.repo.record_intervention(
            "ci_feedback",
            session_id=session["session_id"],
            issue_number=record["issue_number"],
            detail=", ".join(failed),
        )
        self.repo.bump("ci_feedback_sent")
        logger.info(
            "handed CI failure back to session %s (round %s)", session["session_id"], rounds
        )

    async def _feed_review_comment(self, pr_number: int, reviewer: str, comment: str) -> None:
        record = self.repo.pull_request(pr_number)
        if not record or not record["session_id"]:
            return
        session = self.repo.session(record["session_id"])
        if not session or session["closed_at"] is not None:
            # A closed session cannot be revived by a message; forwarding would do nothing.
            self.repo.bump("review_feedback_dropped")
            return

        await self.devin.send_message(
            record["session_id"],
            prompts.review_feedback_message(
                pr_url=record["url"] or "", reviewer=reviewer, comment=comment
            ),
        )
        self.repo.mark_message_sent(record["session_id"])
        self.repo.record_intervention(
            "review_feedback",
            session_id=record["session_id"],
            issue_number=record["issue_number"],
            detail=comment[:500],
        )
        self.repo.bump("review_feedback_sent")

    async def _feed_human_reply(self, issue_number: int, author: str, comment: str) -> None:
        """Forward a human's answer to a blocked session, closing the escalation loop."""
        issue = self.repo.issue(issue_number)
        session = self.repo.latest_session_for_issue(issue_number)
        if not issue or not session:
            return
        if issue["state"] != IssueState.ESCALATED:
            return
        if session["closed_at"] is not None:
            self.repo.bump("human_reply_dropped")
            return

        await self.devin.send_message(
            session["session_id"], prompts.human_reply_message(author=author, comment=comment)
        )
        # Order matters. The grace stamp and the nudge reset must land before the issue returns to
        # `running`, or the next tick re-evaluates a still-`waiting_for_user` session, sees the
        # nudge budget spent, and escalates again — which turned this loop into comment spam.
        self.repo.mark_message_sent(session["session_id"])
        self.repo.clear_nudges(session["session_id"])
        self.repo.set_issue_state(issue_number, IssueState.RUNNING)
        self.repo.record_intervention(
            "human_reply",
            session_id=session["session_id"],
            issue_number=issue_number,
            detail=comment[:500],
        )
        self.repo.bump("human_replies_forwarded")
        try:
            await self.github.remove_label(issue_number, self.settings.escalation_label)
        except Exception:
            logger.debug("could not remove the escalation label from #%s", issue_number)
        logger.info("forwarded human reply to session %s", session["session_id"])

    # -- pass 5: resync -----------------------------------------------------

    async def resync(self) -> None:
        """Recover labeled issues that never reached the queue.

        GitHub does not retry failed webhook deliveries, so anything that arrived while this process
        was down is lost. This pass is the difference between "the webhook is the system" and "the
        webhook is the fast path".
        """
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

    # -- analytics ----------------------------------------------------------

    async def refresh_insights(self) -> None:
        """Reconcile against Devin's Analytics endpoint.

        Two things come from here that a per-session read does not give: the message counts (how
        many turns a fix actually took) and Devin's own size classification.

        The tag filter is sent, but the result is **not** trusted to have applied it. The endpoint
        accepts unknown query parameters without complaint, so a wrong or renamed filter would
        return the whole organization's sessions rather than an error — other people's work would
        quietly land in these metrics. Rows are therefore matched against session ids this
        orchestrator created; `apply_insight` drops anything else. Tags remain what identifies the
        sessions in the Devin dashboard, which is what a reviewer cross-checks.
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
            # Worth counting rather than ignoring: a non-zero value means the tag filter is not
            # doing what the request asked for.
            self.repo.bump("insights_rows_not_ours", foreign)
        logger.info("insights refreshed: %s applied, %s not ours", applied, foreign)

    # -- derived issue state for the dashboard ------------------------------

    def issue_view(self) -> list[dict[str, Any]]:
        rows = []
        for issue in self.repo.issues():
            session = self.repo.latest_session_for_issue(issue["number"])
            pull = self.repo.pr_for_issue(issue["number"])
            phase = classify(session["status"], session["status_detail"]) if session else None
            rows.append(
                {
                    **issue,
                    "phase": phase,
                    "derived_state": issue_state_for(
                        phase,
                        pr_merged=bool(pull and pull["merged_at"]),
                        pr_open=bool(pull and not pull["merged_at"] and not pull["closed_at"]),
                        escalated=issue["state"] == IssueState.ESCALATED,
                        has_session=session is not None,
                    ),
                    "session": session,
                    "pull_request": pull,
                    "structured_output": json.loads(session["structured_output"])
                    if session and session["structured_output"]
                    else None,
                }
            )
        return rows
