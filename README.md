# Autonomous remediation orchestrator

An external service that turns *"a maintainer labeled an issue"* into *"a merged pull request"*,
using the Devin API v3 as the engineer.

It runs against a fork of Apache Superset: [amylase/superset](https://github.com/amylase/superset).

---

## The problem it addresses

Every engineering organisation carries a backlog it never gets to. Security findings, dependency
drift, and small correctness bugs are individually cheap and collectively expensive — and they lose
every prioritisation argument against feature work. The cost is not the fix; it is the context
switch, the diagnosis, and the wait for review.

That backlog is a good fit for an autonomous agent, but only if the agent is wired into the systems
the work already lives in, and only if a leader can see whether it is actually working. This service
is that wiring, plus the reporting.

---

## How it works

```
   ┌─ Event sources (record intent only; zero side effects) ────────┐
   │                                                                │
   │   GitHub webhooks     ─┐                                       │
   │   Devin Schedules     ─┤                                       │
   │   Admin API           ─┤──▶  write desired state to the store   │
   │   Periodic resync     ─┘                                       │
   └───────────────────────────────┬────────────────────────────────┘
                                   ▼
                       ┌───────────────────────┐
                       │   SQLite (state)      │
                       │   desired vs actual   │
                       └───────────┬───────────┘
                                   ▲ ▼
                   ┌───────────────────────────────────┐
                   │  Reconcile loop  (the controller) │
                   │  · diff desired vs actual         │
                   │  · ALL side effects live here     │
                   │  · enforces every policy limit    │
                   └───────┬───────────────────┬───────┘
                           ▼                   ▼
                     Devin API v3          GitHub API
```

The webhook receiver is deliberately thin: it verifies, deduplicates, records intent, and returns.
It never calls the Devin API. Two consequences follow, and they are the reason for the shape:

- **GitHub's 10-second delivery budget stops mattering.** A Devin session takes tens of minutes and
  Superset's CI takes tens more; none of that can happen inside a request.
- **Every limit is enforced in exactly one place.** Concurrency, ACU caps, nudge caps and backoff
  live in the loop, because the loop is the only thing that acts.

It also means the system self-heals. GitHub does **not** retry failed webhook deliveries, so an
event that arrives while this service is down is gone permanently. A five-minute resync pass lists
labeled issues through the API and picks up anything that never arrived. Webhooks are the fast path;
resync is the correct one.

### Event paths

| Event | Purpose |
| --- | --- |
| `issues` / `labeled` (`devin-fix`) | Create a Devin session for the issue |
| `workflow_run` / `completed` (failure) | Hand the CI failure back to the session that opened the PR |
| `pull_request_review_comment` / `created` | Forward reviewer feedback to the session |
| `issue_comment` / `created` | Forward a human's answer to a session blocked on a question |
| `pull_request` / `closed` | Settle the outcome, finalise MTTR |

All five arrive at a single endpoint; `X-GitHub-Event` drives the branch, so signature verification
exists in one place.

### The review-fix loop

A Devin session sleeps automatically after roughly 0.1 ACU of inactivity and **consumes nothing
while asleep**, and `resumable` sessions keep their VM state. That is what makes this affordable:

```
PR opened  →  session sleeps (cost stops)
           →  CI runs (~26 min on this fork)
           →  workflow_run failure webhook
           →  POST /sessions/{id}/messages with the failing check names
           →  session resumes with its working tree intact and self-corrects
```

Waiting out CI is free. The same plumbing carries reviewer comments and human answers.

### When Devin asks a question

A session that stops to ask is a session that has stalled, and stalls are handled in three layers:

1. **Prevent** — acceptance criteria in the issue body, and a playbook policy: *take the most
   conservative option, document the assumption in the PR, continue.* `bypass_approval` removes the
   approval stall entirely.
2. **Nudge, bounded** — at most `MAX_NUDGES` generic messages restating that policy. The cap is not
   optional: without it, ask → nudge → ask burns ACUs until the per-session ceiling trips, and that
   ceiling is meant to be a backstop rather than the control loop.
3. **Escalate, visibly** — comment the question on the issue, apply `needs-human`, show it in its
   own dashboard lane, and **keep the MTTR clock running**. A human's reply is forwarded back to the
   session, which resumes where it stopped.

Blocked sessions are a third outcome category — neither success nor failure — and the dashboard
reports them as such.

---

## Observability

The dashboard is at `/`, refreshes every 15 seconds, and answers one question: *is this working?*

**Headline**

| Metric | Why this one |
| --- | --- |
| Resolution rate | Issues closed by a merged PR, denominated **per issue**. An issue that needed three sessions and got fixed is one success, not one in three. |
| Time to merge, split three ways | Devin / CI / human review, separately. A single number hides where the time goes — and the split shows the bottleneck is not the agent. |
| Cost per resolution | Total ACUs **including sessions that never merged**, over issues actually resolved. Success-only cost accounting is a vanity metric. |
| Autonomy rate | Completed with zero human intervention. Automatic nudges count as intervention. |
| Merge rate | Merged ÷ opened pull requests. |

**Below the fold** — session outcome breakdown by `status_detail`, intervention counts by kind, CI
first-pass rate, Devin turns per resolution, webhook delivery and dedup counters, retry counts.

Cost and effort figures are reconciled against Devin's Analytics endpoint
(`GET /v3/organizations/{org}/sessions/insights`), which also supplies the message counts and size
classification a per-session read does not carry. The tag filter is sent but **not trusted to have
been applied**: that endpoint accepts unknown query parameters without complaint, so a renamed or
mistyped filter would return the whole organization instead of an error, and other people's sessions
would quietly enter these numbers. Rows are matched against session ids this orchestrator created,
and anything else is counted and discarded. Tags remain what identifies the sessions in the Devin
dashboard — which is what a reviewer cross-checks — but they are not what the arithmetic trusts.

Money figures rest on an ACU price and an assumed manual effort per issue. Both are printed on the
page as assumptions, along with the sample size, because a rate over five issues shown without `n`
costs more credibility than it buys.

`GET /api/metrics` returns the same numbers as JSON.

---

## Production-shaped concerns

| Concern | How |
| --- | --- |
| Delivery dedup | The `X-GitHub-Delivery` GUID **and** `sha256(body)`. The GUID catches GitHub's redelivery; the body hash catches a replay, which the GUID cannot because it is an unsigned header |
| Attempt dedup | The attempt counter is incremented before the billable call, so a failure mid-flight can never bill the same attempt twice |
| Retries | Exponential backoff with jitter on `429` and `5xx`, honouring `Retry-After` |
| Concurrency | Bounded concurrent sessions |
| Cost caps | `max_acu_limit` per session, plus a global ACU budget checked before every start |
| Escalation | Nudge cap → `needs-human` → issue comment → dashboard lane |
| Restart recovery | State is in SQLite; in-flight sessions are rebuilt from it on boot |
| Lost-event recovery | Five-minute resync against the GitHub API |
| Stale-event rejection | Trust-but-verify: the issue is re-fetched before any ACUs are spent |

### Webhook security

Signature verification is over the **raw body**, uses constant-time comparison, and treats a missing
`X-Hub-Signature-256` header as a rejection rather than a skip — GitHub omits the header entirely
when no secret is configured, so "verify only if present" is bypassable by simply not sending it.
The legacy SHA-1 header is refused.

GitHub sends no timestamp header, so there is no signature freshness window to enforce; replay
defence is the delivery-id store.

A verified signature proves GitHub sent the event, not that the event is still true. Before creating
a session the orchestrator re-fetches the issue and confirms the label is still applied — a label
added and immediately removed should not cost anything.

---

## Running it

```bash
cp .env.example .env    # then fill in the four credentials
docker compose up --build
```

Dashboard on <http://localhost:8000/>, webhook endpoint at `POST /webhooks/github`.

Register the playbook and the weekly scan schedule once:

```bash
python scripts/bootstrap_devin.py
```

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `DEVIN_API_KEY` | — | Service-user token (`cog_…`) |
| `DEVIN_ORG_ID` | — | Organization id (`org-…`) |
| `GITHUB_TOKEN` | — | Fine-grained PAT: Issues RW, Pull requests RW, Contents R |
| `WEBHOOK_SECRET` | — | Must match the secret configured on the repository webhook |
| `GITHUB_REPO` | `amylase/superset` | Target repository |
| `TRIGGER_LABEL` | `devin-fix` | Label that starts a remediation |
| `MAX_CONCURRENT_SESSIONS` | `2` | Concurrency cap |
| `MAX_ACU_PER_SESSION` | `20` | Per-session cost ceiling |
| `GLOBAL_ACU_BUDGET` | `250` | Total spend ceiling |
| `MAX_NUDGES` | `2` | Automatic nudges before escalating to a human |
| `MAX_CI_FEEDBACK_ROUNDS` | `3` | Self-correction attempts before escalating |
| `ADMIN_TOKEN` | unset | Enables `/api/admin/*`; disabled when unset |

### Exposing the webhook

Only `POST /webhooks/github` needs to be reachable from GitHub. The quick command below exposes
the **whole app**, including the dashboard and `/api/*`, which are unauthenticated:

```bash
cloudflared tunnel --url http://localhost:8000
```

That is fine for a short demo on a throwaway hostname and wrong for anything longer — `/api/issues`
returns Devin's `root_cause` write-ups for security fixes whose pull requests have not merged yet.
For anything beyond a demo, tunnel only the webhook path or put a token in front of the reads.

Then on the repository: **Settings → Webhooks → Add webhook**, payload URL
`https://<tunnel>/webhooks/github`, content type `application/json`, the same secret as
`WEBHOOK_SECRET`, SSL verification on, and subscribe to *Issues*, *Issue comments*, *Pull requests*,
*Pull request review comments*, and *Workflow runs*.

---

## Tests

```bash
pip install -e ".[dev]"
pytest
```

238 tests covering the orchestrator itself, with no network and no credentials required:

- **The loop, end to end** — label → session → pull request → completion comment → merge, driven
  against recording doubles and a real database. Also: the concurrency cap actually holding back a
  third session, concurrent ticks not double-spending, a failure after a billable call never
  re-spending, bounded nudges, escalation written exactly once, a human reply resuming instead of
  re-escalating, one CI feedback round per commit, resync recovering an undelivered event.
- **Signature verification** — valid, tampered, missing header, empty header, legacy SHA-1, wrong
  secret, and the router verifying the exact bytes received (written as a literal non-canonical
  body, because a fixture built with `json.dumps` cannot express that bug).
- **Receiver behaviour** — redelivery with the same GUID creates one session, not two; non-trigger
  labels and non-`labeled` actions do nothing; another repository is ignored even when correctly
  signed; only trusted `author_association` values can put text in front of the agent.
- **Clients** — retry, exponential backoff, `Retry-After`, 4xx raised rather than swallowed;
  check-run pagination and every failing conclusion; the create-session payload, including the tags;
  the message-list shape.
- **State interpretation** — a finished task still reports `running`; a suspended session is asleep,
  not failed; an unknown suspension degrades to sleep rather than inflating the failure rate.
- **Policy** — every cap pinned at its boundary.
- **Metrics** — resolution rate denominated per issue; cost per resolution including failed
  sessions; the three-way MTTR split; empty state reporting `None` rather than a misleading 0%; and
  one seam test that runs the metric functions over rows a real `Repo` wrote, so a schema rename
  cannot break the dashboard while every unit test stays green.
- **Persistence** — re-labelling does not reset the MTTR clock; the blocked state is latched before
  it decays into sleep; recorded spend is never lowered by a later poll.

One invariant is deliberately **not** claimed as tested: `hmac.compare_digest` cannot be pinned by
assertion, because replacing it with `==` changes only timing. It is enforced by reading the code,
not by a test that would give false comfort.

Remediation code changes are tested separately, in Superset's own CI on each pull request.

---

## CI on the fork: measured baseline

Remediation PRs are judged on **Superset's CI, unmodified** — no scoped or bespoke workflow.
"The tests pass in Apache Superset's CI" is something a reviewer can verify; "the tests pass in a
workflow I wrote" is not.

Two things had to be established first, and both are worth knowing if you reproduce this.

**Actions do not simply work on a fork.** The repository reported `actions.enabled: true` while
*zero* workflows were registered and pull requests triggered nothing at all. Re-applying the Actions
permission through the API registered all 45. Six then remained in `disabled_fork` state — every one
of them carrying a `schedule:` trigger. `pre-commit` (lint and format) and `codeql` were enabled
explicitly because they matter here; the remaining four (image mirroring, scheduled image refresh,
showtime cleanup, merge-conflict labelling) are irrelevant to this project and left disabled.

**Some checks are red before any of our code lands.** [PR #1] on the fork adds a single
self-contained test file and nothing else, so it is a clean measurement of the fork's own CI health:

| | |
| --- | --- |
| Total checks | 52 |
| Wall clock | **26 minutes** (versus ~110 upstream — no runner queue contention on a fork) |
| Failed on first attempt | `dependency-review`, `playwright-tests (chromium)` + its `-required` anchor |

Re-running both told them apart, which is the whole reason to take a baseline rather than assume:

- **`dependency-review` — deterministic.** Failed identically on both attempts. It needs repository
  infrastructure a fork does not have. Expect it red on every remediation PR.
- **`playwright-tests` — flaky.** Attempt 1 failed inside Superset's own soft-delete E2E suite with
  `werkzeug.exceptions.MethodNotAllowed: 405` on `/log/`. Attempt 2 passed on the identical commit.
  A red playwright check on a remediation PR therefore needs a re-run before it means anything.

This is the difference between attributing a red check and guessing at it. The failing job is left
failing rather than disabled, and the review-fix loop is capped at
`MAX_CI_FEEDBACK_ROUNDS` so a flake cannot send Devin into an unbounded chase.

[PR #1]: https://github.com/amylase/superset/pull/1

---

## Limitations

Stated plainly, because a system whose reporting hides its own gaps is not worth the reporting.

- **Single instance.** SQLite and an in-process loop. Correct at this scale; horizontal scaling
  would need a real queue and leader election.
- **The issue body is not fenced against prompt injection.** The orchestrator fences everything
  it forwards — comments, and the issue title in the session prompt — but Devin reads the issue
  body itself from the URL, and on a public fork anyone can write it. The only control there is
  an in-band instruction telling Devin to treat the body as evidence rather than instructions.
  Closing it properly means fetching the body server-side and passing it fenced; that is not
  built.
- **The dashboard and read API are unauthenticated**, and the tunnel command above publishes
  them. See the note in that section.
- **Effects are recorded after they succeed, not reserved before.** A crash between an API call
  and its record repeats that effect once — a duplicate comment, at worst a duplicate nudge.
  This is deliberate: the exactly-once machinery it replaced derived keys from mutable counters
  and could wedge an issue permanently, which is a worse failure in a system a human watches.
  Session creation is protected separately, by incrementing the attempt before it spends.
- **A retry request outranks a running session.** Re-applying the label starts a fresh attempt
  even if the previous session is still working; the old session is not stopped, because the
  v3 API exposes no terminate (probed: `/terminate` and `/stop` are 404). It sleeps at ~0.1 ACU
  and is capped by `MAX_ACU_PER_SESSION`, so the cost is bounded but not zero.
- **Polling, not push.** No Devin push callbacks were found in the v3 documentation, so session
  state is pulled on a 10-second cadence, as the official examples do.
- **No per-day billing breakdown.** The enterprise consumption endpoints
  (`/v3/enterprise/consumption/*`) return 403 for an org-scoped service user (verified), so cost is
  built from `acus_consumed` — read per session and reconciled against the Analytics endpoint.
- **The `repos` value format is unverified.** Devin's documentation gives `repos` no description and
  no example anywhere; the CLI consistently uses `owner/repo`, which is what is sent here. The first
  real session settles it by showing which repository was attached.
- **Server-side tag filtering on Analytics is unverified**, which is why it is not relied on. See
  the note in Observability.
- **Money and time-saved numbers rest on stated assumptions**, printed on the dashboard rather than
  buried.
- **Small sample.** Rates are computed over a handful of issues and are indicative, not statistical.

---

## Layout

```
app/
  main.py                 FastAPI app, routes, loop startup, dashboard
  config.py               the operating envelope, from the environment
  webhooks/
    verify.py             HMAC over the raw body, constant-time, fail-closed
    router.py             POST /webhooks/github — verify, dedup, record, return
    handlers.py           event -> intent (pure function)
  clients/
    devin.py              v3: sessions, messages, insights, playbooks, schedules
    github.py             issues, comments, labels, pull requests, check runs
    http.py               retry with exponential backoff and jitter
  core/
    orchestrator.py       the reconcile loop; decides, never writes
    effects.py            the only writer: Devin and GitHub side effects
    state.py              facts -> issue status and session liveness, pure
    metrics.py            metric arithmetic, no I/O
    prompts.py            everything the orchestrator says to Devin
  db/
    schema.sql  repo.py   SQLite; timestamps are the point
scripts/
  bootstrap_devin.py      register the playbook and the weekly schedule
tests/                    238 tests, no network required
```
