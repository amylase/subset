"""The webhook receiver.

Deliberately thin. It verifies, deduplicates, records intent, and returns — nothing here calls the
Devin API or spends money. That is what keeps every response inside GitHub's 10 second budget and
keeps the policy limits enforceable in exactly one place (the reconcile loop).

Order matters: verify the signature against the raw body *before* parsing, and treat a missing
signature header as a rejection rather than a skip.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request, Response

from app.db.repo import Repo
from app.webhooks import handlers
from app.webhooks.verify import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    SignatureError,
    verify_signature,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/webhooks/github")
async def github_webhook(request: Request) -> Response:
    settings = request.app.state.settings
    repo: Repo = request.app.state.repo

    raw = await request.body()
    try:
        verify_signature(settings.webhook_secret, raw, request.headers.get(SIGNATURE_HEADER))
    except SignatureError as exc:
        repo.bump("webhook_rejected")
        logger.warning("rejected webhook delivery: %s", exc)
        return Response(status_code=401, content=str(exc))

    event = request.headers.get(EVENT_HEADER, "")
    delivery = request.headers.get(DELIVERY_HEADER, "")
    if not delivery:
        repo.bump("webhook_rejected")
        return Response(status_code=400, content="missing X-GitHub-Delivery header")

    if event == "ping":
        return Response(status_code=200, content="pong")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        repo.bump("webhook_rejected")
        return Response(status_code=400, content="payload is not JSON")

    action = payload.get("action")

    # Redeliveries reuse the original GUID, so this catches both GitHub's redelivery button and a
    # replayed request. GitHub sends no timestamp header, so this store is the only
    # replay defence available.
    if not repo.record_delivery(delivery, event, action):
        repo.bump("webhook_duplicates")
        logger.info("duplicate delivery %s (%s/%s) ignored", delivery, event, action)
        return Response(status_code=200, content="duplicate delivery ignored")

    # Reject deliveries for a repository we are not responsible for, even when correctly signed.
    full_name = (payload.get("repository") or {}).get("full_name")
    if full_name and full_name != settings.github_repo:
        repo.bump("webhook_wrong_repo")
        return Response(status_code=202, content="not our repository")

    intent = handlers.to_intent(event, payload, trigger_label=settings.trigger_label)
    if intent is None:
        return Response(status_code=202, content="no action for this event")

    kind, data = intent
    repo.enqueue(kind, data)
    repo.bump("webhook_accepted")
    logger.info("queued %s from delivery %s", kind, delivery)
    return Response(status_code=202, content=f"queued: {kind}")
