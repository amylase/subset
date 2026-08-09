"""What the orchestrator says to Devin.

The governing idea: hand over a well-scoped problem and the standard of done, then get out of the
way. Nothing here describes *how* to fix anything. The issue body carries the evidence and the
acceptance criteria; the diagnosis and the design of the fix are Devin's job, and prescribing them
would both waste the capability and produce worse fixes.

The second idea is the ambiguity policy. A session that stops to ask a question is a session that
has stalled, and the cheapest place to prevent that is in the instructions: prefer the conservative
option, write the assumption down, keep going.
"""

from __future__ import annotations

AMBIGUITY_POLICY = """\
If you hit ambiguity, do not stop to ask. Take the most conservative option that satisfies the \
acceptance criteria, record the decision and your reasoning under an "Assumptions" heading in the \
pull request description, and continue. Stop and ask only when the decision genuinely depends on \
product requirements that cannot be inferred from the codebase."""

PLAYBOOK_NAME = "Superset autonomous remediation"

PLAYBOOK_BODY = f"""\
# Superset autonomous remediation

You are remediating a defect in a fork of apache/superset. Work autonomously and end with a pull \
request that a maintainer would merge.

## Standard of done

1. Diagnose the root cause before changing anything. If the reported cause turns out to be wrong, \
   say so in the pull request and fix the real one.
2. Make the smallest change that genuinely fixes the root cause. A narrow diff also keeps CI fast, \
   because Superset's workflows skip jobs by changed path.
3. Add tests that fail before your change and pass after it. Put unit tests under \
   `tests/unit_tests/` so they run without a database.
4. Run the relevant tests locally before opening the pull request.
5. Open one pull request per issue, and reference the issue number in the description.
6. Write a pull request description covering: the root cause, why this fix addresses it, what the \
   tests prove, and any assumptions.

## Constraints

- Do not change public behaviour beyond what the issue asks for.
- Do not modify CI workflows, lint configuration, or test infrastructure to make checks pass. If a \
  check fails, fix the code or explain honestly why the failure is not caused by your change.
- Match the surrounding code's style; the repository is linted by pre-commit.

## Ambiguity

{AMBIGUITY_POLICY}

## Honesty

If you cannot fully fix the issue, say so plainly in the structured output and in \
the pull request. \
A partial fix with an accurate description is more useful than an overstated one.
"""


def session_prompt(*, repo: str, issue_number: int, issue_title: str, issue_url: str) -> str:
    """The task handed to a new session.

    Kept short on purpose. The issue body already contains the evidence, reproduction and
    acceptance criteria, and duplicating it here would only create a second source of truth that
    can drift.
    """
    return f"""\
Remediate issue #{issue_number} in the repository `{repo}`.

Issue: {issue_title}
{issue_url}

Read the issue in full. It contains the evidence, the expected behaviour, and the acceptance
criteria that define done. Diagnose the root cause yourself rather than assuming the report is
complete or correct.

When you are finished, open a pull request against `master` in `{repo}` that satisfies every
acceptance criterion in the issue, and provide your structured output.

{AMBIGUITY_POLICY}

Superset's own CI runs on the pull request, unmodified. Do not weaken, skip, or reconfigure any
check in order to make it pass.
"""


def nudge_message() -> str:
    """Sent when a session stops to ask a question.

    Generic on purpose. Answering the actual question would mean steering Devin's design decisions
    from the outside, which is both worse engineering and, in a system meant to demonstrate
    autonomy, self-defeating. This restates the policy and hands the decision back.
    """
    return f"""\
Continue without waiting for an answer.

{AMBIGUITY_POLICY}

If you have genuinely reached a decision that cannot be made from the codebase, say so explicitly \
and stop; a human will pick it up.
"""


def ci_failure_message(*, pr_url: str, failed_checks: list[str], round_number: int) -> str:
    """Hand a CI failure back to the session that produced the pull request.

    This is the review-fix loop. The session still holds the VM state that produced the branch, so
    it resumes with the working tree intact instead of reconstructing context.
    """
    checks = "\n".join(f"- {name}" for name in failed_checks) or "- (no named check reported)"
    return f"""\
CI failed on your pull request ({round_number}).

{pr_url}

Failing checks:
{checks}

Investigate the failures, fix the cause, and push to the same branch. If a failure is not caused by
your change — for example a job that cannot run on a fork because it needs repository secrets — say
so explicitly instead of working around it.
"""


def review_feedback_message(*, pr_url: str, reviewer: str, comment: str) -> str:
    return f"""\
A reviewer left feedback on your pull request.

{pr_url}

From @{reviewer}:
\"\"\"
{comment}
\"\"\"

Address the feedback and push to the same branch. If you disagree, reply on the pull request with
your reasoning rather than silently ignoring it.
"""


def human_reply_message(*, author: str, comment: str) -> str:
    return f"""\
A human answered the question you were blocked on.

From @{author}:
\"\"\"
{comment}
\"\"\"

Continue from here.
"""


SCAN_SCHEDULE_PROMPT = """\
Audit the default branch of this repository for newly introduced defects worth remediating: \
dependency vulnerabilities, unsafe deserialization or injection patterns, and correctness bugs \
reachable from backend code paths.

Do not fix anything. For each finding you are confident about, open a GitHub issue containing the \
evidence (file and line), the reproduction, the expected behaviour, and acceptance criteria \
including a test. Apply the label matching its class. Skip anything already covered by an open \
issue.

Prefer three well-evidenced findings over ten speculative ones.
"""
