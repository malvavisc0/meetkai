"""Sentry (GlitchTip) observability initialization.

Initializes the Sentry SDK for the current execution context (cockpit, CLI,
or bot). Initialization is a no-op when ``SENTRY_DSN`` is unset.

Configuration is read from the environment:

- ``SENTRY_DSN`` — DSN pointing at the GlitchTip instance. Empty disables Sentry.
- ``SENTRY_ENVIRONMENT`` — overrides ``default_environment`` when set.
- ``SENTRY_RELEASE`` — release identifier sent with every event.
"""

import logging
import os
from typing import Any

import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration

logger = logging.getLogger(__name__)


def init_sentry(
    default_environment: str,
    *,
    extra_integrations: list[Any] | None = None,
    profiles_sample_rate: float = 0.0,
) -> None:
    """Initialize the Sentry SDK if ``SENTRY_DSN`` is configured.

    Args:
        default_environment: Value used for ``environment`` when the
            ``SENTRY_ENVIRONMENT`` env var is not set (e.g. ``"cockpit"``).
        extra_integrations: Additional ``sentry_sdk`` integrations to enable
            (e.g. ``FastApiIntegration`` for the cockpit).
        profiles_sample_rate: Fraction of transactions to profile (0.0–1.0).

    Raises:
        Exception: Re-raises any error from ``sentry_sdk.init`` so that
            misconfigured observability fails fast at startup.
    """
    dsn = os.getenv("SENTRY_DSN", "")
    if not dsn:
        return

    integrations: list[Any] = [
        LoggingIntegration(
            level=logging.INFO,
            event_level=logging.WARNING,
        ),
        *(extra_integrations or []),
    ]

    sentry_sdk.init(
        dsn=dsn,
        environment=os.getenv("SENTRY_ENVIRONMENT", default_environment),
        release=os.getenv("SENTRY_RELEASE", "unknown"),
        integrations=integrations,
        traces_sample_rate=0.01,
        profiles_sample_rate=profiles_sample_rate,
        auto_session_tracking=False,  # GlitchTip doesn't support sessions
        attach_stacktrace=False,  # Only capture stacks for actual exceptions
    )
