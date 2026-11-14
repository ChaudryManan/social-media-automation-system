"""Redis-backed fixed-window rate limiting and cooldown for password reset."""

import hashlib
import logging

import redis as redis_lib

from app.core.config import settings
from app.core.errors import RateLimitError, ServiceUnavailableError

logger = logging.getLogger(__name__)

# Lua script: atomically INCR + EXPIRE only on first call within the window.
# Returns the new counter value.
_INCR_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current
"""

# Lua script: SET if not exists with TTL (atomic).
# Returns 1 if key was set (cooldown not active), 0 if key already existed.
_SET_NX_SCRIPT = """
return redis.call('SET', KEYS[1], '1', 'NX', 'EX', ARGV[1])
"""


def _get_redis() -> redis_lib.Redis:
    """Return a Redis client using the configured REDIS_URL."""
    return redis_lib.Redis.from_url(
        settings.REDIS_URL,
        socket_connect_timeout=1,
        socket_timeout=1,
        decode_responses=True,
    )


def _email_key(email: str) -> str:
    """Return a safe, anonymised key fragment derived from a lowercased email."""
    return hashlib.sha256(email.lower().encode()).hexdigest()[:32]


def check_password_reset_rate_limits(ip: str, email: str) -> None:
    """Enforce fixed-window rate limits and per-account cooldown for password reset.

    Checks (in order):
      1. Per-IP fixed-window limit   (PW_RESET_IP_LIMIT / PW_RESET_IP_WINDOW_SECONDS)
      2. Per-account cooldown        (SET NX EX PW_RESET_COOLDOWN_SECONDS)
      3. Per-account fixed-window (PW_RESET_ACCT_LIMIT / PW_RESET_ACCT_WINDOW_SECONDS)

    Raises:
        RateLimitError: when any limit is breached.
        ServiceUnavailableError: when Redis is not reachable (fail-closed).
    """
    try:
        client = _get_redis()
        client.ping()
    except redis_lib.RedisError as exc:
        logger.error(
            "Redis unavailable during password-reset rate-limit check: %s", exc
        )
        raise ServiceUnavailableError(
            message=(
                "Password reset service is temporarily unavailable. "
                "Please try again shortly."
            ),
            code="service_unavailable",
        ) from exc

    try:
        incr_script = client.register_script(_INCR_SCRIPT)
        nx_script = client.register_script(_SET_NX_SCRIPT)

        # 1. Per-IP fixed-window
        ip_key = f"pw_reset:ip:{ip}"
        ip_count = incr_script(
            keys=[ip_key], args=[settings.PW_RESET_IP_WINDOW_SECONDS]
        )
        if ip_count > settings.PW_RESET_IP_LIMIT:
            logger.warning(
                "Password-reset IP rate limit exceeded",
                extra={"ip": ip, "count": ip_count},
            )
            raise RateLimitError(
                message="Too many password reset requests from this address.",
                code="rate_limit_exceeded",
            )

        email_hash = _email_key(email)

        # 2. Per-account cooldown (SET NX EX)
        cooldown_key = f"pw_reset:cooldown:{email_hash}"
        set_result = nx_script(
            keys=[cooldown_key], args=[settings.PW_RESET_COOLDOWN_SECONDS]
        )
        if set_result is None:
            # Key already existed: cooldown active
            logger.warning(
                "Password-reset cooldown active for account",
                extra={"email_hash": email_hash},
            )
            raise RateLimitError(
                message="Please wait before requesting another reset.",
                code="rate_limit_cooldown",
            )

        # 3. Per-account fixed-window
        acct_key = f"pw_reset:acct:{email_hash}"
        acct_count = incr_script(
            keys=[acct_key], args=[settings.PW_RESET_ACCT_WINDOW_SECONDS]
        )
        if acct_count > settings.PW_RESET_ACCT_LIMIT:
            logger.warning(
                "Password-reset account rate limit exceeded",
                extra={"email_hash": email_hash, "count": acct_count},
            )
            raise RateLimitError(
                message="Too many password reset requests for this account.",
                code="rate_limit_exceeded",
            )

    except (RateLimitError, ServiceUnavailableError):
        raise
    except redis_lib.RedisError as exc:
        logger.error("Redis error during rate-limit enforcement: %s", exc)
        raise ServiceUnavailableError(
            message=(
                "Password reset service is temporarily unavailable. "
                "Please try again shortly."
            ),
            code="service_unavailable",
        ) from exc
