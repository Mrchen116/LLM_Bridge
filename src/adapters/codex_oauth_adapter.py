from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict


async def collect_with_retry(
    *,
    collect_once: Callable[[Dict[str, str]], Awaitable[Dict[str, Any]]],
    headers: Dict[str, str],
    max_retries: int,
    max_failovers: int = 0,
    is_retryable: Callable[[int], bool],
    refresh_headers: Callable[[], Awaitable[Dict[str, str]]],
    on_retryable_response: Callable[[Dict[str, str], int, str], Awaitable[None]] | None = None,
    should_retry_result: Callable[[Dict[str, Any]], bool] | None = None,
    should_failover_result: Callable[[Dict[str, Any]], bool] | None = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    current_headers = headers
    current_account_attempts = 0
    failover_count = 0
    retry_sequence = 0

    while True:
        result = await collect_once(current_headers)
        current_account_attempts += 1
        status_code = int(result.get("status_code") or 0)
        retryable = should_retry_result(result) if should_retry_result is not None else is_retryable(status_code)
        if not retryable:
            return result

        should_failover = (
            should_failover_result(result)
            if should_failover_result is not None
            else False
        )

        if should_failover and failover_count < max_failovers:
            if on_retryable_response is not None:
                await on_retryable_response(current_headers, status_code, str(result.get("error_text") or ""))
            await asyncio.sleep(1 * (2 ** min(retry_sequence, 3)))
            current_headers = await refresh_headers()
            failover_count += 1
            current_account_attempts = 0
            retry_sequence += 1
            continue

        if current_account_attempts < max_retries:
            await asyncio.sleep(1 * (2 ** min(retry_sequence, 3)))
            retry_sequence += 1
            continue

        return result
