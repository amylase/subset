"""The receiver end to end: verify, deduplicate, filter, record intent, return fast."""

from __future__ import annotations

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from tests.conftest import REPO_FULL_NAME, SECRET


def post(app, event: str, payload: dict, *, delivery: str = "d-1", secret: str = SECRET):
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    with TestClient(app) as client:
        return client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Event": event,
                "X-GitHub-Delivery": delivery,
                "X-Hub-Signature-256": signature,
                "Content-Type": "application/json",
            },
        )


def labeled(number: int = 42, label: str = "devin-fix") -> dict:
    return {
        "action": "labeled",
        "label": {"name": label},
        "issue": {"number": number},
        "repository": {"full_name": REPO_FULL_NAME},
    }


def test_valid_delivery_is_accepted_and_queued(webhook_app, repo):
    response = post(webhook_app, "issues", labeled())
    assert response.status_code == 202
    pending = repo.pending_queue()
    assert [(p["kind"], p["payload"]) for p in pending] == [("issue_labeled", {"number": 42})]


def test_tampered_payload_is_rejected(webhook_app, repo):
    body = json.dumps(labeled()).encode()
    signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    with TestClient(webhook_app) as client:
        response = client.post(
            "/webhooks/github",
            content=body.replace(b'"number": 42', b'"number": 43'),
            headers={
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "d-x",
                "X-Hub-Signature-256": signature,
            },
        )
    assert response.status_code == 401
    assert repo.pending_queue() == []


def test_missing_signature_is_rejected(webhook_app, repo):
    with TestClient(webhook_app) as client:
        response = client.post(
            "/webhooks/github",
            content=json.dumps(labeled()).encode(),
            headers={"X-GitHub-Event": "issues", "X-GitHub-Delivery": "d-y"},
        )
    assert response.status_code == 401
    assert repo.pending_queue() == []


def test_redelivery_with_the_same_guid_queues_only_once(webhook_app, repo):
    """GitHub reuses the delivery GUID on redelivery, which is what makes this dedup work.

    Without it, pressing "Redeliver" would create a second Devin session for the same issue and
    spend ACUs twice.
    """
    first = post(webhook_app, "issues", labeled(), delivery="same-guid")
    second = post(webhook_app, "issues", labeled(), delivery="same-guid")

    assert first.status_code == 202
    assert second.status_code == 200
    assert len(repo.pending_queue()) == 1
    assert repo.counters().get("webhook_duplicates") == 1


def test_a_different_label_does_not_trigger(webhook_app, repo):
    response = post(webhook_app, "issues", labeled(label="documentation"))
    assert response.status_code == 202
    assert repo.pending_queue() == []


def test_unlabeled_action_does_not_trigger(webhook_app, repo):
    payload = labeled()
    payload["action"] = "unlabeled"
    post(webhook_app, "issues", payload, delivery="d-unlabeled")
    assert repo.pending_queue() == []


def test_another_repository_is_ignored_even_when_correctly_signed(webhook_app, repo):
    payload = labeled()
    payload["repository"]["full_name"] = "someone/else"
    response = post(webhook_app, "issues", payload, delivery="d-other")
    assert response.status_code == 202
    assert repo.pending_queue() == []
    assert repo.counters().get("webhook_wrong_repo") == 1


def test_ping_is_answered(webhook_app):
    response = post(webhook_app, "ping", {"zen": "hello"}, delivery="d-ping")
    assert response.status_code == 200


def test_the_router_verifies_the_exact_bytes_received(webhook_app, repo):
    """Signed over the received bytes, not over a re-serialisation of the parsed payload.

    The verify-level test for this is a tautology at the router's expense: every other test here
    builds its body with ``json.dumps``, so ``json.dumps(json.loads(raw)) == raw`` and a router that
    verified the re-serialised form would still pass. A real GitHub body is compact and does not
    survive that round trip, so this case is written as literal bytes.
    """
    body = (
        b'{"action":"labeled",\n  "issue":{"number":42},"label":{"name":"devin-fix"},'
        b'"repository":{"full_name":"amylase/superset"}}'
    )
    assert json.dumps(json.loads(body)).encode() != body
    signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()

    with TestClient(webhook_app) as client:
        response = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "d-raw",
                "X-Hub-Signature-256": signature,
            },
        )
    assert response.status_code == 202
    assert [p["payload"] for p in repo.pending_queue()] == [{"number": 42}]


def test_missing_delivery_header_is_rejected(webhook_app, repo):
    body = json.dumps(labeled()).encode()
    signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    with TestClient(webhook_app) as client:
        response = client.post(
            "/webhooks/github",
            content=body,
            headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": signature},
        )
    assert response.status_code == 400
