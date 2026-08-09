"""The reconcile loop.

Everything with a side effect happens here. The webhook receiver only records intent, which is what
makes the concurrency, ACU, and nudge limits enforceable in one place instead of being re-argued at
every call site.

The loop compares desired state ("this issue is labeled, so it should end up fixed") against actual
state (session phase, pull request state) and closes the gap. Four passes run at different cadences
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
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from app.clients.devin import DevinClient, last_devin_message
from app.clients.github import GitHubClient
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
    TERMINAL_PHASES,
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

    # -- lifecycle ----------------------------------------------------------

    async def run_forever(self) -> None:
        interval = self.settings.session_poll_interval
        pr_every = max(1, int(self.settings.pr_poll_interval / interval))
        resync_every = max(1, int(self.settings.resync_interval / interval))

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
        self._tick += 1
        await self.drain_queue()
        await self.start_sessions()
        await self.track_sessions()
        if self._tick % pr_every == 0:
            await self.track_pull_requests()
        if self._tick % resync_every == 1:
            await self.resync()

    # -- pass 1: act on recorded intent ------------------------------------

    async def drain_queue(self) -> None:
        for item in self.repo.pending_queue():
            try:
                await self._handle_queued(item["kind"], item["payload"])
            except Exception:
                logger.exception("queue item %s (%s) failed", item["id"], item["kind"])
                self.repo.bump("queue_errors")
            finally:
                # Marked dispatched either way: a poison item must not wedge the queue, and the
                # polling passes re-derive CI and merge state independently.
                self.repo.mark_dispatched(item["id"])

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
                record = next(
                    (
                        p
                        for p in self.repo.pull_requests()
                        if p["pr_number"] == payload["pr_number"]
                    ),
                    None,
                )
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
        created = self.repo.upsert_issue(number, issue.get("title", ""), klass, now())
        if created:
            logger.info("registered issue #%s (%s)", number, klass)
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
            if not is_startable(phase):
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

            await self._create_session(issue)

    def _active_session_count(self) -> int:
        return sum(
            1
            for s in self.repo.sessions()
            if classify(s["status"], s["status_detail"]) in ACTIVE_PHASES
        )

    async def _create_session(self, issue: dict[str, Any]) -> None:
        number = issue["number"]
        issue_url = f"https://github.com/{self.settings.github_repo}/issues/{number}"
        tags = [
            ORCHESTRATOR_TAG,
            f"repo:{self.settings.github_repo}",
            f"issue:{number}",
        ]
        if issue.get("klass"):
            tags.append(issue["klass"])

        response = await self.devin.create_session(
            prompts.session_prompt(
                repo=self.settings.github_repo,
                issue_number=number,
                issue_title=issue.get("title", ""),
                issue_url=issue_url,
            ),
            title=f"superset#{number}: {issue.get('title', '')}"[:120],
            tags=tags,
            repo=f"https://github.com/{self.settings.github_repo}",
            max_acu_limit=self.settings.max_acu_per_session,
            playbook_id=self._playbook_id,
        )
        session_id = response["session_id"]
        self.repo.create_session(session_id, number, response.get("url"), tags)
        self.repo.set_issue_state(number, IssueState.RUNNING)
        self.repo.bump("sessions_created")

        await self.github.comment(
            number,
            f"🤖 Devin session started for this issue.\n\n"
            f"- Session: {response.get('url')}\n"
            f"- Tags: `{'`, `'.join(tags)}`\n"
            f"- ACU cap: {self.settings.max_acu_per_session}\n\n"
            f"Progress will be reported here.",
        )
        logger.info("session %s started for issue #%s", session_id, number)

    # -- pass 3: track sessions --------------------------------------------

    async def track_sessions(self) -> None:
        for record in self.repo.sessions():
            phase = classify(record["status"], record["status_detail"])
            if phase in TERMINAL_PHASES and record["finished_at"]:
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
        phase = classify(status, detail)
        structured = remote.get("structured_output")
        pulls = remote.get("pull_requests") or []
        done = is_work_done(
            phase, has_structured_output=bool(structured), has_pull_request=bool(pulls)
        )

        changed = self.repo.update_session(
            session_id,
            status=status,
            status_detail=detail,
            acus=float(remote.get("acus_consumed") or 0),
            structured_output=structured,
            blocked=phase is Phase.BLOCKED,
            finished=done,
        )
        if changed:
            logger.info("session %s -> %s/%s", session_id, status, detail)

        if pulls:
            await self._link_pull_request(record, pulls)

        if phase is Phase.BLOCKED:
            await self._handle_blocked(record, session_id)
        elif phase is Phase.HALTED_COST:
            await self._escalate(
                record, f"Devin stopped: `{detail}`. The cost or quota ceiling was reached."
            )
        elif phase is Phase.FAILED:
            self.repo.set_issue_state(record["issue_number"], IssueState.FAILED)
            self.repo.bump("sessions_failed")
        elif done and not record["finished_at"]:
            await self._report_completion(record, structured, pulls)

    async def _link_pull_request(self, record: dict[str, Any], pulls: list[dict[str, Any]]) -> None:
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
        await self._escalate(
            record,
            "Devin is blocked on a question and the automatic nudge limit "
            f"({self.envelope.max_nudges}) is exhausted."
            + (f"\n\n> {question}" if question else ""),
        )

    async def _escalate(self, record: dict[str, Any], reason: str) -> None:
        number = record["issue_number"]
        issue = self.repo.issue(number)
        if issue and issue["state"] == IssueState.ESCALATED:
            return

        self.repo.set_issue_state(number, IssueState.ESCALATED)
        self.repo.record_intervention(
            "escalation", session_id=record["session_id"], issue_number=number, detail=reason
        )
        self.repo.bump("escalations")
        await self.github.add_label(number, self.settings.escalation_label)
        await self.github.comment(
            number,
            f"🙋 **Human input needed**\n\n{reason}\n\n"
            f"Session: {record['url']}\n\n"
            "Reply on this issue and the answer will be forwarded to the "
            "session, which will resume "
            "from where it stopped.",
        )
        logger.info("escalated issue #%s", number)

    async def _report_completion(
        self, record: dict[str, Any], structured: Any, pulls: list[dict[str, Any]]
    ) -> None:
        number = record["issue_number"]
        data = structured if isinstance(structured, dict) else {}
        outcome = data.get("outcome", "unknown")
        summary = data.get("summary", "")
        root_cause = data.get("root_cause", "")
        tests = data.get("tests_added") or []
        assumptions = data.get("assumptions") or []
        follow_up = data.get("follow_up") or ""

        lines = [f"✅ **Devin finished** — outcome: `{outcome}`", ""]
        if root_cause:
            lines += [f"**Root cause.** {root_cause}", ""]
        if summary:
            lines += [summary, ""]
        for pull in pulls:
            lines.append(f"- Pull request: {pull.get('pr_url')} (`{pull.get('pr_state')}`)")
        if tests:
            lines += ["", "**Tests added.**"] + [f"- `{t}`" for t in tests]
        if assumptions:
            lines += ["", "**Assumptions.**"] + [f"- {a}" for a in assumptions]
        if follow_up:
            lines += ["", f"**Left undone.** {follow_up}"]
        lines += [
            "",
            f"ACUs consumed: {record['acus']:.2f} · Session: {record['url']}",
        ]
        await self.github.comment(number, "\n".join(lines))
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
                self.repo.set_issue_state(record["issue_number"], IssueState.MERGED)
            self.repo.bump("prs_merged")
            logger.info("PR #%s merged", number)
            return
        if pull.get("state") == "closed":
            self.repo.update_pr(number, state="closed", closed_at=now())
            self.repo.bump("prs_closed_unmerged")
            return

        settled, conclusion = await self.github.checks_settled(pull["head"]["sha"])
        if settled and not record["ci_settled_at"]:
            self.repo.update_pr(number, ci_settled_at=now(), ci_conclusion=conclusion)
        if settled and conclusion == "failure":
            await self._feed_ci_failure(number, pull["head"]["sha"])

    # -- review-fix loop ----------------------------------------------------

    async def _feed_ci_failure(self, pr_number: int, sha: str | None) -> None:
        record = next(
            (p for p in self.repo.pull_requests() if p["pr_number"] == pr_number),
            None,
        )
        if not record or not record["session_id"]:
            return
        session = self.repo.session(record["session_id"])
        if not session:
            return
        if not should_send_ci_feedback(self.envelope, rounds_used=session["ci_rounds"]):
            await self._escalate(
                session,
                f"CI is still failing on {record['url']} after "
                f"{self.envelope.max_ci_feedback_rounds} self-correction attempts.",
            )
            return

        if not sha:
            pull = await self.github.get_pull(pr_number)
            sha = pull["head"]["sha"]
        failed = await self.github.failed_check_summary(sha)
        if not failed:
            return

        rounds = session["ci_rounds"] + 1
        await self.devin.send_message(
            session["session_id"],
            prompts.ci_failure_message(
                pr_url=record["url"] or "", failed_checks=failed, round_number=rounds
            ),
        )
        self.repo.bump_session(session["session_id"], "ci_rounds")
        self.repo.update_pr(pr_number, ci_attempts=rounds)
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
        record = next((p for p in self.repo.pull_requests() if p["pr_number"] == pr_number), None)
        if not record or not record["session_id"]:
            return
        await self.devin.send_message(
            record["session_id"],
            prompts.review_feedback_message(
                pr_url=record["url"] or "", reviewer=reviewer, comment=comment
            ),
        )
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

        await self.devin.send_message(
            session["session_id"], prompts.human_reply_message(author=author, comment=comment)
        )
        self.repo.set_issue_state(issue_number, IssueState.RUNNING)
        self.repo.record_intervention(
            "human_reply",
            session_id=session["session_id"],
            issue_number=issue_number,
            detail=comment[:500],
        )
        self.repo.bump("human_replies_forwarded")
        await self.github.remove_label(issue_number, self.settings.escalation_label)
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
            await self._register_issue(issue["number"])

    # -- derived issue state for the dashboard ------------------------------

    def issue_view(self) -> list[dict[str, Any]]:
        rows = []
        for issue in self.repo.issues():
            session = self.repo.latest_session_for_issue(issue["number"])
            pull = self.repo.pr_for_issue(issue["number"])
            phase = (
                classify(session["status"], session["status_detail"]) if session else Phase.STARTING
            )
            rows.append(
                {
                    **issue,
                    "phase": phase,
                    "derived_state": issue_state_for(
                        phase,
                        pr_merged=bool(pull and pull["merged_at"]),
                        pr_open=bool(pull and not pull["merged_at"] and not pull["closed_at"]),
                        escalated=issue["state"] == IssueState.ESCALATED,
                    ),
                    "session": session,
                    "pull_request": pull,
                    "structured_output": json.loads(session["structured_output"])
                    if session and session["structured_output"]
                    else None,
                }
            )
        return rows
