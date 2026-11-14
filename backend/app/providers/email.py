"""Email sender interface and provider implementations.

Providers are selected via the EMAIL_PROVIDER setting.
Supported values: "console" (default, development only).

Production SMTP or other providers can be added behind this interface without
changing any call sites.
"""

import logging
from abc import ABC, abstractmethod

from app.core.config import settings

logger = logging.getLogger(__name__)


class BaseEmailSender(ABC):
    """Abstract email sender interface."""

    @abstractmethod
    def send_password_reset(
        self,
        to_email: str,
        raw_token: str,
        reset_base_url: str,
    ) -> None:
        """Send a password-reset email containing the reset link.

        The raw_token must NOT be logged by any implementation.
        Args:
            to_email: Recipient address.
            raw_token: The unencoded reset token (included in the link only).
            reset_base_url: Frontend reset page URL; token appended as query param.
        """


class ConsoleEmailSender(BaseEmailSender):
    """Development email sender: prints reset link to stdout/logs.

    The raw token is included only in the console output, NOT in logger calls,
    so it is never captured by structured log pipelines.
    """

    def send_password_reset(
        self,
        to_email: str,
        raw_token: str,
        reset_base_url: str,
    ) -> None:
        reset_link = f"{reset_base_url}?token={raw_token}"
        logger.info(
            "Password-reset email dispatched (console sender)",
            extra={"to": to_email},
            # raw_token and reset_link are intentionally NOT logged here
        )
        # Print to stdout for local development inspection only.
        # In production this code path is never reached.
        print(  # noqa: T201
            f"\n{'=' * 60}\n"
            f"[DEV] Password Reset Email\n"
            f"To: {to_email}\n"
            f"Link: {reset_link}\n"
            f"{'=' * 60}\n"
        )


def get_email_sender() -> BaseEmailSender:
    """Factory: return the configured email sender instance."""
    provider = settings.EMAIL_PROVIDER.lower()
    if provider == "console":
        return ConsoleEmailSender()
    # Future providers: elif provider == "smtp": return SmtpEmailSender()
    logger.warning(
        "Unknown EMAIL_PROVIDER '%s'; falling back to console sender.", provider
    )
    return ConsoleEmailSender()
