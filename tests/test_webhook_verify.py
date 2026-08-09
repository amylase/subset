"""Signature verification.

The failure modes tested here are the ones that turn a signed webhook into an open endpoint: a
missing header treated as "nothing to check", the legacy SHA-1 header accepted as a fallback, and
verification performed against re-serialised JSON instead of the received bytes.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from app.webhooks.verify import SignatureError, expected_signature, verify_signature

SECRET = "s3cret"
BODY = b'{"action":"labeled","issue":{"number":7}}'


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_accepts_a_correct_signature():
    verify_signature(SECRET, BODY, sign(SECRET, BODY))


def test_rejects_a_tampered_body():
    signature = sign(SECRET, BODY)
    tampered = BODY.replace(b'"number":7', b'"number":9')
    with pytest.raises(SignatureError, match="mismatch"):
        verify_signature(SECRET, tampered, signature)


def test_rejects_a_missing_header():
    # GitHub omits the header entirely when no secret is configured, so "verify only if present"
    # would be bypassable by simply not sending it.
    with pytest.raises(SignatureError, match="missing"):
        verify_signature(SECRET, BODY, None)


def test_rejects_an_empty_header():
    with pytest.raises(SignatureError, match="missing"):
        verify_signature(SECRET, BODY, "")


def test_rejects_the_legacy_sha1_header():
    sha1 = "sha1=" + hmac.new(SECRET.encode(), BODY, hashlib.sha1).hexdigest()
    with pytest.raises(SignatureError, match="unsupported"):
        verify_signature(SECRET, BODY, sha1)


def test_rejects_a_signature_made_with_another_secret():
    with pytest.raises(SignatureError, match="mismatch"):
        verify_signature(SECRET, BODY, sign("not-the-secret", BODY))


def test_signature_is_over_raw_bytes_not_reserialised_json():
    """Re-serialising the payload changes the bytes and must not still verify.

    This is the bug that fails intermittently — whitespace and key order sometimes happen to match —
    so it is worth pinning explicitly.
    """
    signature = expected_signature(SECRET, BODY)
    reserialised = json.dumps(json.loads(BODY), indent=2).encode()
    assert reserialised != BODY
    with pytest.raises(SignatureError):
        verify_signature(SECRET, reserialised, signature)
