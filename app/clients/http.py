"""Shared HTTP behaviour for the two API clients.

Retries with exponential backoff on ``429`` and ``5xx``. Devin's API documents no rate limits but
its own examples handle ``429``, so the safe assumption is that limits exist and are undocumented.
``Retry-After`` is honoured when present because a server-provided delay beats a guessed one.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx

logger = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:400]}")
        self.status = status
        self.body = body


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_attempts: int = 5,
    base_delay: float = 1.0,
    on_retry: Any = None,
    **kwargs: Any,
) -> httpx.Response:
    """Issue a request, retrying transient failures with exponential backoff and jitter.

    :param on_retry: optional zero-argument callback invoked once per retry, so the reliability
        tier of the dashboard can report how often the orchestrator had to back off.
    """
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            response = await client.request(method, url, **kwargs)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last_exc = exc
        else:
            if response.status_code not in RETRY_STATUSES:
                if response.status_code >= 400:
                    raise ApiError(response.status_code, response.text)
                return response
            last_exc = ApiError(response.status_code, response.text)
            retry_after = _retry_after(response)
            if retry_after is not None and attempt < max_attempts - 1:
                if on_retry:
                    on_retry()
                await asyncio.sleep(retry_after)
                continue

        if attempt == max_attempts - 1:
            break
        if on_retry:
            on_retry()
        delay = base_delay * (2**attempt) + random.uniform(0, base_delay)  # noqa: S311
        logger.warning("retrying %s %s in %.1fs (%s)", method, url, delay, last_exc)
        await asyncio.sleep(delay)

    assert last_exc is not None
    raise last_exc


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None
