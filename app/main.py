"""Application entry point.

One process: the webhook receiver, the dashboard and the reconcile loop share a database and an
event loop. At this scale that is the right call — a broker and a worker pool would add operational
surface without buying anything — but it does mean a single instance. Scaling out would need a real
queue and leader election, which is stated as a limitation rather than pretended away.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.clients.devin import DevinClient
from app.clients.github import GitHubClient
from app.config import get_settings
from app.core import metrics as metrics_mod
from app.core.effects import Effects
from app.core.orchestrator import Orchestrator
from app.db.repo import Repo
from app.webhooks.router import router as webhook_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
logger = logging.getLogger("app")

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "dashboard" / "templates"))


def _fmt_duration(seconds: float | None) -> str:
    """Human-readable duration. ``None`` renders as an em dash, not a misleading zero."""
    if seconds is None:
        return "—"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _fmt_usd(value: float | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


TEMPLATES.env.filters["dur"] = _fmt_duration
TEMPLATES.env.filters["pct"] = _fmt_pct
TEMPLATES.env.filters["usd"] = _fmt_usd


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    repo = Repo(settings.db_path)

    devin = DevinClient(
        settings.devin_api_key, settings.org_base, on_retry=lambda: repo.bump("devin_retries")
    )
    github = GitHubClient(
        settings.github_token,
        settings.github_api_base,
        settings.github_repo,
        on_retry=lambda: repo.bump("github_retries"),
    )
    effects = Effects(settings, repo, devin, github)

    app.state.settings = settings
    app.state.repo = repo
    app.state.devin = devin
    app.state.github = github
    app.state.effects = effects
    app.state.orchestrator = Orchestrator(settings, repo, effects)

    # Resolve our own GitHub identity before accepting anything. Until this is known, comments the
    # orchestrator wrote itself are indistinguishable from a maintainer's and would be forwarded
    # back to Devin as human answers.
    app.state.own_login = settings.self_login
    if not app.state.own_login:
        try:
            app.state.own_login = await github.whoami()
        except Exception:
            logger.exception("could not resolve the orchestrator's own GitHub login")
    logger.info("orchestrator identity: %s", app.state.own_login or "unknown")
    if not app.state.own_login:
        logger.warning(
            "own login unknown: comments written by this service may be forwarded back to Devin. "
            "Set SELF_LOGIN to make this deterministic."
        )

    task = asyncio.create_task(app.state.orchestrator.run_forever(), name="reconcile-loop")
    logger.info(
        "orchestrator started: repo=%s trigger=%r concurrency=%s acu_cap=%s/session budget=%s",
        settings.github_repo,
        settings.trigger_label,
        settings.max_concurrent_sessions,
        settings.max_acu_per_session,
        settings.global_acu_budget,
    )
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await devin.aclose()
        await github.aclose()


app = FastAPI(title="Devin remediation orchestrator", lifespan=lifespan)
app.include_router(webhook_router)

api = APIRouter(prefix="/api")


def _metrics(request: Request) -> metrics_mod.Metrics:
    repo: Repo = request.app.state.repo
    settings = request.app.state.settings
    return metrics_mod.compute(
        view=request.app.state.orchestrator.issue_view(),
        sessions=repo.sessions(),
        pull_requests=repo.pull_requests(),
        interventions=repo.interventions(),
        notifications=repo.notifications(),
        counters=repo.counters(),
        acu_unit_cost_usd=settings.acu_unit_cost_usd,
        manual_effort_hours_per_issue=settings.manual_effort_hours_per_issue,
        engineer_hourly_usd=settings.engineer_hourly_usd,
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@api.get("/metrics")
async def metrics_endpoint(request: Request) -> JSONResponse:
    return JSONResponse(asdict(_metrics(request)))


@api.get("/issues")
async def issues_endpoint(request: Request) -> JSONResponse:
    return JSONResponse(request.app.state.orchestrator.issue_view())


@api.post("/admin/reconcile")
async def force_reconcile(
    request: Request, x_admin_token: str | None = Header(default=None)
) -> dict[str, Any]:
    """Run one reconcile tick immediately, so a demo need not wait on the natural cadence."""
    _require_admin(request, x_admin_token)
    await request.app.state.orchestrator.tick(pr_every=1, slow_every=1)
    return {"status": "ticked"}


@api.post("/admin/issues/{number}")
async def force_register(
    request: Request, number: int, x_admin_token: str | None = Header(default=None)
) -> dict[str, Any]:
    """Register an issue as if its trigger label had just been applied.

    Goes through the same trust-but-verify check as the webhook path: if the label is not actually
    on the issue, nothing happens.
    """
    _require_admin(request, x_admin_token)
    request.app.state.repo.enqueue("issue_labeled", {"number": number}, provenance="system")
    return {"status": "queued", "issue": number}


def _require_admin(request: Request, token: str | None) -> None:
    expected = request.app.state.settings.admin_token
    if not expected:
        raise HTTPException(status_code=404, detail="admin API disabled (ADMIN_TOKEN unset)")
    # Encoded before comparing: `compare_digest` raises on non-ASCII `str`, and Starlette decodes
    # headers as latin-1, so a non-ASCII token would 500 and become an enabled/disabled oracle.
    supplied = (token or "").encode("utf-8", "ignore")
    if not hmac.compare_digest(supplied, expected.encode("utf-8")):
        request.app.state.repo.bump("admin_auth_failures")
        logger.warning("rejected an admin request with a bad token")
        raise HTTPException(status_code=401, detail="bad admin token")


app.include_router(api)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    settings = request.app.state.settings
    return TEMPLATES.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "m": _metrics(request),
            "issues": request.app.state.orchestrator.issue_view(),
            "repo": settings.github_repo,
        },
    )
