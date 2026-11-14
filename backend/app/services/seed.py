"""Idempotent database seeding service for admin account and system settings."""

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.security import hash_password
from app.models.limits import UserLimits
from app.models.settings import SystemSettings
from app.models.users import User

logger = logging.getLogger(__name__)

# Canonical system setting defaults per DATABASE.md Section 4.17
DEFAULT_SYSTEM_SETTINGS: list[dict[str, Any]] = [
    {
        "key": "max_retries_default",
        "value": 3,
        "description": "Default maximum generation retry attempts per agent node.",
    },
    {
        "key": "max_retries_scene",
        "value": 2,
        "description": "Maximum retry attempts for individual video scene generation.",
    },
    {
        "key": "max_review_cycles",
        "value": 2,
        "description": "Maximum reviewer-writer fix iterations before escalation.",
    },
    {
        "key": "lead_time_minutes_post",
        "value": 30,
        "description": "Minutes before publish_at to trigger social post generation.",
    },
    {
        "key": "lead_time_minutes_video",
        "value": 120,
        "description": "Minutes before publish_at to trigger video generation.",
    },
    {
        "key": "default_max_workspaces",
        "value": 3,
        "description": "Default maximum workspaces allowed per user.",
    },
    {
        "key": "default_max_accounts_per_workspace",
        "value": 5,
        "description": "Default maximum social accounts allowed per workspace.",
    },
    {
        "key": "default_monthly_generation_limit",
        "value": 100,
        "description": "Default monthly content generation request quota per user.",
    },
    {
        "key": "metrics_schedule",
        "value": {"intervals_hours": [2, 6, 24, 48, 168]},
        "description": "Scheduled post-publish metric collection intervals in hours.",
    },
    {
        "key": "model_text",
        "value": "gemini-2.0-flash",
        "description": "Default primary LLM model identifier for text generation.",
    },
    {
        "key": "model_fast",
        "value": "gemini-2.0-flash-lite",
        "description": "Default lightweight model identifier for routing and checks.",
    },
    {
        "key": "platform_rules",
        "value": {
            "facebook": {
                "max_caption_length": 63206,
                "allowed_formats": ["post"],
            },
            "instagram": {
                "max_caption_length": 2200,
                "max_hashtags": 30,
                "allowed_formats": ["post", "video"],
            },
            "tiktok": {
                "max_caption_length": 2200,
                "max_video_duration_seconds": 600,
                "allowed_formats": ["video"],
            },
            "youtube": {
                "max_title_length": 100,
                "max_description_length": 5000,
                "allowed_formats": ["video"],
            },
        },
        "description": "Per-platform content constraints and media limits.",
    },
]


def seed_admin(
    db: Session,
    email: str | None = None,
    password: str | None = None,
) -> User:
    """Ensure an administrator user exists, creating one idempotently if missing.

    Requires SEED_ADMIN_EMAIL and SEED_ADMIN_PASSWORD in environment settings.
    Fails safely if credentials are not configured or if the email belongs
    to a user who is not already an admin (automatic promotion is forbidden).
    """
    seed_email = (
        email if email is not None else settings.SEED_ADMIN_EMAIL or ""
    ).strip()
    seed_password = (
        password if password is not None else settings.SEED_ADMIN_PASSWORD or ""
    ).strip()

    if not seed_email:
        raise ValueError(
            "SEED_ADMIN_EMAIL is missing or empty. "
            "Admin seeding requires an administrator email configured in environment."
        )

    if not seed_password:
        raise ValueError(
            "SEED_ADMIN_PASSWORD is missing or empty. "
            "Admin seeding requires an admin password configured in environment."
        )

    clean_email = seed_email.lower()
    existing_user = db.scalar(
        select(User).where(func.lower(User.email) == clean_email)
    )

    if existing_user is None:
        password_hash = hash_password(seed_password)
        admin_user = User(
            email=clean_email,
            password_hash=password_hash,
            full_name="System Administrator",
            role="admin",
            is_active=True,
        )
        db.add(admin_user)
        db.flush()

        limits = UserLimits(
            user_id=admin_user.id,
            max_workspaces=100,
            max_accounts_per_workspace=50,
            monthly_generation_limit=10000,
        )
        db.add(limits)
        db.commit()
        db.refresh(admin_user)
        logger.info(
            "Admin user seeded successfully",
            extra={"email": clean_email, "user_id": str(admin_user.id)},
        )
        return admin_user

    # Account already exists: verify role
    if existing_user.role != "admin":
        raise ValueError(
            f"User '{clean_email}' already exists with role '{existing_user.role}'. "
            "Automatic role promotion to platform administrator is forbidden."
        )

    logger.info(
        "Admin user already exists and holds admin role; leaving account unchanged",
        extra={"email": clean_email, "user_id": str(existing_user.id)},
    )
    return existing_user


def seed_system_settings(db: Session) -> int:
    """Insert default system settings only if they do not already exist.

    Returns the number of settings newly inserted.
    """
    inserted_count = 0

    for setting_def in DEFAULT_SYSTEM_SETTINGS:
        key = setting_def["key"]
        existing = db.get(SystemSettings, key)
        if existing is None:
            new_setting = SystemSettings(
                key=key,
                value=setting_def["value"],
                description=setting_def["description"],
            )
            db.add(new_setting)
            inserted_count += 1

    if inserted_count > 0:
        db.commit()
        logger.info(f"Seeded {inserted_count} new system settings.")
    else:
        logger.info("All default system settings already present; skipping insertion.")

    return inserted_count


def seed_all(db: Session) -> dict[str, Any]:
    """Execute complete initial seeding process."""
    admin = seed_admin(db)
    inserted_settings = seed_system_settings(db)
    return {
        "admin_email": admin.email,
        "admin_id": str(admin.id),
        "new_settings_count": inserted_settings,
    }


def main() -> None:
    """CLI execution entrypoint for make seed."""
    logging.basicConfig(level=logging.INFO)
    logger.info("Starting database seeding process...")
    db = SessionLocal()
    try:
        result = seed_all(db)
        logger.info(
            f"Database seeding completed successfully: "
            f"admin={result['admin_email']} ({result['admin_id']}), "
            f"new_settings={result['new_settings_count']}"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
