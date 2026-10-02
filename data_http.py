from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx


log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 418, 425, 429, 500, 502, 503, 504}


async def request_with_retry(
    http: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    attempts: int = 5,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    before_attempt: Callable[[], Awaitable[None]] | None = None,
    retry_response: Callable[[httpx.Response], bool] | None = None,
    **kwargs,
) -> httpx.Response:
    """HTTP request with bounded exponential backoff for transport/timeout/429/5xx failures.

    Non-retryable 4xx responses are returned immediately and handled by raise_for_status()
    at the call site. Retry-After is respected when present.
    """
    last_exc: Exception | None = None
    attempts = max(1, attempts)
    for attempt in range(attempts):
        if before_attempt is not None:
            await before_attempt()
        try:
            if hasattr(http, "request"):
                response = await http.request(method, url, **kwargs)
            elif method.upper() == "GET" and hasattr(http, "get"):
                response = await http.get(url, **kwargs)
            else:
                raise AttributeError("HTTP client must provide request() or get()")
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            if attempt >= attempts - 1:
                raise
            delay = min(max_delay, base_delay * (2**attempt))
            log.warning("HTTP transport retry %s/%s for %s in %.2fs: %s", attempt + 1, attempts, url, delay, type(exc).__name__)
            await asyncio.sleep(delay)
            continue

        status_code = int(getattr(response, "status_code", 200))
        if status_code not in RETRYABLE_STATUS and not (retry_response and retry_response(response)):
            return response
        if attempt >= attempts - 1:
            return response

        headers = getattr(response, "headers", {}) or {}
        retry_after = headers.get("Retry-After")
        delay = min(max_delay, base_delay * (2**attempt))
        if retry_after is not None:
            try:
                server_delay = float(retry_after)
            except (ValueError, TypeError):
                try:
                    date = parsedate_to_datetime(retry_after)
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)
                    server_delay = (date - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    server_delay = 0.0
            if math.isfinite(server_delay):
                # max_delay limits our backoff, never a server's required wait.
                delay = max(delay, server_delay)
        log.warning("HTTP status %s retry %s/%s for %s in %.2fs", status_code, attempt + 1, attempts, url, delay)
        await asyncio.sleep(delay)

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("request retry loop exited unexpectedly")
