from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, Optional

import httpx


async def post_with_retry(
    *,
    upstream_url: str,
    request_body: Dict[str, Any],
    headers: Dict[str, str],
    max_retries: int,
    is_retryable: Callable[[int], bool],
    refresh_headers: Callable[[], Awaitable[Dict[str, str]]],
    on_retryable_response: Optional[Callable[[Dict[str, str], int, str], Awaitable[None]]] = None,
    verify: bool,
    timeout_seconds: float,
    trust_env: bool,
) -> httpx.Response:
    async with httpx.AsyncClient(
        verify=verify,
        timeout=httpx.Timeout(timeout_seconds),
        trust_env=trust_env,
    ) as client:
        r: Optional[httpx.Response] = None
        last_retry_response: Optional[httpx.Response] = None
        current_headers = headers
        for attempt in range(max_retries):
            r = await client.post(upstream_url, headers=current_headers, json=request_body)
            if not is_retryable(r.status_code):
                break
            last_retry_response = r
            if attempt < max_retries - 1:
                if on_retryable_response is not None:
                    await on_retryable_response(current_headers, r.status_code, r.text or "")
                await asyncio.sleep(1 * (2 ** attempt))
                current_headers = await refresh_headers()

        if r is None:
            raise RuntimeError("post_with_retry failed to produce response")
        if is_retryable(r.status_code) and last_retry_response is not None:
            return last_retry_response
        return r
