"""
Resilient 3-attempt retry utilities with exponential backoff & Sentry reporting.

Wraps external I/O operations (Supabase DB, Playwright browser actions, ROV Academy
HTTP requests) so transient network glitches are automatically retried up to
`max_attempts` (default: 3) before logging, capturing to Sentry, and re-raising.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any, Callable, Tuple, Type, TypeVar

from tenacity import (
    AsyncRetrying,
    RetryCallState,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from backend.monitoring import capture_error

logger = logging.getLogger("quiz_arena.retry")

F = TypeVar("F", bound=Callable[..., Any])


def _log_retry_attempt(component: str, operation: str) -> Callable[[RetryCallState], None]:
    """Build a tenacity `before_sleep` callback that logs each retry warning clearly."""

    def _callback(retry_state: RetryCallState) -> None:
        outcome = retry_state.outcome
        err = outcome.exception() if outcome else None
        next_wait = retry_state.next_action.sleep if retry_state.next_action else 0.0
        logger.warning(
            "[Retry %s:%s] Attempt %d failed (%s: %s). Retrying in %.2fs...",
            component,
            operation,
            retry_state.attempt_number,
            type(err).__name__ if err else "Error",
            err,
            next_wait,
        )

    return _callback


def retry_sync(
    *,
    component: str,
    operation: str,
    max_attempts: int = 3,
    min_wait: float = 1.0,
    max_wait: float = 4.0,
    retry_exceptions: Tuple[Type[Exception], ...] = (Exception,),
) -> Callable[[F], F]:
    """
    Decorator for synchronous external I/O (e.g., Playwright actions, sync DB calls).

    Retries up to `max_attempts` times with exponential backoff. If all attempts fail,
    captures the final exception to Sentry and re-raises it cleanly.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            retrying = Retrying(
                stop=stop_after_attempt(max_attempts),
                wait=wait_exponential(multiplier=max(min_wait, 0.05), min=min_wait, max=max_wait),
                retry=retry_if_exception_type(retry_exceptions),
                before_sleep=_log_retry_attempt(component, operation),
                reraise=True,
            )
            try:
                for attempt in retrying:
                    with attempt:
                        return func(*args, **kwargs)
            except Exception as exc:
                capture_error(
                    exc,
                    component=component,
                    operation=operation,
                    extra={"max_attempts": max_attempts},
                )
                raise

        return wrapper  # type: ignore[return-value]

    return decorator


def retry_async(
    *,
    component: str,
    operation: str,
    max_attempts: int = 3,
    min_wait: float = 1.0,
    max_wait: float = 4.0,
    retry_exceptions: Tuple[Type[Exception], ...] = (Exception,),
) -> Callable[[F], F]:
    """
    Decorator for async external I/O (e.g., Supabase REST/SDK queries, HTTP fetches).

    Uses `tenacity.AsyncRetrying` so event-loop tasks never block while backing off.
    Reports final failure to Sentry before re-raising.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            retrying = AsyncRetrying(
                stop=stop_after_attempt(max_attempts),
                wait=wait_exponential(multiplier=max(min_wait, 0.05), min=min_wait, max=max_wait),
                retry=retry_if_exception_type(retry_exceptions),
                before_sleep=_log_retry_attempt(component, operation),
                reraise=True,
                sleep=asyncio.sleep,
            )
            try:
                async for attempt in retrying:
                    with attempt:
                        return await func(*args, **kwargs)
            except Exception as exc:
                capture_error(
                    exc,
                    component=component,
                    operation=operation,
                    extra={"max_attempts": max_attempts},
                )
                raise

        return wrapper  # type: ignore[return-value]

    return decorator


async def execute_with_async_retry(
    coro_factory: Callable[[], Any],
    *,
    component: str,
    operation: str,
    max_attempts: int = 3,
    min_wait: float = 1.0,
    max_wait: float = 4.0,
    retry_exceptions: Tuple[Type[Exception], ...] = (Exception,),
) -> Any:
    """
    Functional async retry helper for dynamic retry counts/waits sourced from Settings.

    `coro_factory` is a zero-argument callable returning a fresh coroutine on each try.
    """
    retrying = AsyncRetrying(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=max(min_wait, 0.05), min=min_wait, max=max_wait),
        retry=retry_if_exception_type(retry_exceptions),
        before_sleep=_log_retry_attempt(component, operation),
        reraise=True,
        sleep=asyncio.sleep,
    )
    try:
        async for attempt in retrying:
            with attempt:
                return await coro_factory()
    except Exception as exc:
        capture_error(
            exc,
            component=component,
            operation=operation,
            extra={"max_attempts": max_attempts},
        )
        raise
