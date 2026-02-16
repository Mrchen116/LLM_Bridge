from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict


async def collect_with_retry(
    *,
    collect_once: Callable[[Dict[str, str]], Awaitable[Dict[str, Any]]],
    headers: Dict[str, str],
    max_retries: int,
    is_retryable: Callable[[int], bool],
    refresh_headers: Callable[[], Awaitable[Dict[str, str]]],
) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    last_retry_result: Dict[str, Any] | None = None
    current_headers = headers
    for attempt in range(max_retries):
        result = await collect_once(current_headers)
        status_code = int(result.get("status_code") or 0)
        if not is_retryable(status_code):
            break
        last_retry_result = result
        if attempt < max_retries - 1:
            await asyncio.sleep(1 * (2 ** attempt))
            current_headers = await refresh_headers()

    status_code = int(result.get("status_code") or 0)
    if is_retryable(status_code) and last_retry_result is not None:
        return last_retry_result
    return result
