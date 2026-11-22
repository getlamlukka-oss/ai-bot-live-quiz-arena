"""
Sentry observability & structured logging integration.

Initializes `sentry_sdk` with both `StarletteIntegration` and `FastApiIntegration`
(required for full request & exception tracing on FastAPI/Starlette stacks) and
exposes `capture_error()` to annotate and forward exceptions from Database,
WebSocket, Scraper, and AI Bot layers.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

from backend.config import Settings

logger = logging.getLogger("quiz_arena")
_SENTRY_INITIALIZED = False


def configure_logging() -> logging.Logger:
    """Configure consistent structured console logging for backend and scraper."""
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    return logger


def init_sentry(settings: Settings) -> bool:
    """
    Initialize Sentry SDK when SENTRY_DSN is configured.

    Returns True if Sentry was actively initialized, False if running in local-only
    logging mode without a remote DSN.
    """
    global _SENTRY_INITIALIZED
    configure_logging()

    if not settings.has_sentry_config:
        logger.info("Sentry DSN not configured — error tracking running in local logging mode.")
        return False

    try:
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.app_env,
            traces_sample_rate=settings.sentry_traces_sample_rate,
            integrations=[
                StarletteIntegration(transaction_style="endpoint"),
                FastApiIntegration(transaction_style="endpoint"),
            ],
            send_default_pii=False,
        )
        _SENTRY_INITIALIZED = True
        logger.info("Sentry SDK initialized for environment '%s'.", settings.app_env)
        return True
    except Exception as exc:
        logger.warning("Failed to initialize Sentry SDK: %s", exc)
        return False


def capture_error(
    exc: BaseException,
    *,
    component: str,
    operation: str,
    extra: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    Record an exception in both local logs and Sentry with component/operation tags.

    Using an isolated Sentry scope guarantees extra context from one failed WebSocket
    or Supabase request never leaks onto unrelated concurrent requests.
    """
    context_payload = {
        "component": component,
        "operation": operation,
        **(extra or {}),
    }
    logger.error(
        "[%s:%s] %s: %s | context=%s",
        component,
        operation,
        type(exc).__name__,
        exc,
        context_payload,
    )

    try:
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("component", component)
            scope.set_tag("operation", operation)
            scope.set_context("arena_context", context_payload)
            return sentry_sdk.capture_exception(exc)
    except Exception as sentry_exc:
        logger.debug("Sentry capture skipped/failed: %s", sentry_exc)
        return None
