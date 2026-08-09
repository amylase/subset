"""GitHub webhook signature verification.

Three things here are load-bearing and easy to get wrong:

1. **The raw body is what is signed.** Verifying a re-serialised parsed payload fails on key order
   and whitespace, and it fails *intermittently*, which makes it painful to diagnose. Nothing in
   this module accepts parsed JSON.
2. **A missing signature header is a rejection, not a skip.** GitHub omits
   ``X-Hub-Signature-256`` entirely when no secret is configured, so "verify only if present" can be
   bypassed by simply not sending the header.
3. **Constant-time comparison.** GitHub's own documentation calls this out.

GitHub sends no timestamp header, so there is no signature freshness window to enforce here.
Replay defence lives in the delivery-id store instead — see ``app.db.repo.record_delivery``.
"""

from __future__ import annotations

import hashlib
import hmac

SIGNATURE_HEADER = "X-Hub-Signature-256"
LEGACY_SIGNATURE_HEADER = "X-Hub-Signature"
DELIVERY_HEADER = "X-GitHub-Delivery"
EVENT_HEADER = "X-GitHub-Event"

_PREFIX = "sha256="


class SignatureError(Exception):
    """Raised when a delivery cannot be attributed to GitHub."""


def expected_signature(secret: str, body: bytes) -> str:
    """The value ``X-Hub-Signature-256`` should carry for ``body``."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return _PREFIX + digest


def verify_signature(secret: str, body: bytes, signature: str | None) -> None:
    """Verify a delivery signature, raising :class:`SignatureError` if it does not hold.

    :param secret: the webhook secret configured on the repository
    :param body: the exact bytes received, before any parsing
    :param signature: the ``X-Hub-Signature-256`` header value, or ``None`` if absent
    """
    if not signature:
        raise SignatureError("missing X-Hub-Signature-256 header")
    if not signature.startswith(_PREFIX):
        # Anything else is either the legacy SHA-1 header or a forgery. SHA-1 is not accepted.
        raise SignatureError("unsupported signature algorithm")
    if not hmac.compare_digest(expected_signature(secret, body), signature):
        raise SignatureError("signature mismatch")
