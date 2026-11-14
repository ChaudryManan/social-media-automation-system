"""Unit tests for workspace request schemas."""

import pytest
from app.schemas.workspaces import WorkspaceCreate, WorkspaceUpdate
from pydantic import ValidationError


def test_create_applies_defaults_and_strips_name() -> None:
    """Only the name is required; defaults match the database defaults."""
    data = WorkspaceCreate(name="  Brand A  ")

    assert data.name == "Brand A"
    assert data.default_language == "en"
    assert data.timezone == "UTC"
    assert data.description is None
    assert data.brand_voice is None
    assert data.default_platforms is None
    assert data.settings is None


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
def test_create_rejects_blank_or_too_long_name(name: str) -> None:
    with pytest.raises(ValidationError):
        WorkspaceCreate(name=name)


def test_create_requires_name() -> None:
    with pytest.raises(ValidationError):
        WorkspaceCreate()  # type: ignore[call-arg]


def test_create_blank_text_becomes_none() -> None:
    data = WorkspaceCreate(name="A", description="   ", brand_voice="")
    assert data.description is None
    assert data.brand_voice is None


@pytest.mark.parametrize("timezone", ["Asia/Karachi", "Europe/London", "UTC"])
def test_create_accepts_valid_timezones(timezone: str) -> None:
    assert WorkspaceCreate(name="A", timezone=timezone).timezone == timezone


@pytest.mark.parametrize("timezone", ["Mars/Base", "", "Asia / Karachi", "../etc"])
def test_create_rejects_invalid_timezones(timezone: str) -> None:
    with pytest.raises(ValidationError):
        WorkspaceCreate(name="A", timezone=timezone)


@pytest.mark.parametrize("language", ["en", "ur", "pt-BR", "roman-ur"])
def test_create_accepts_language_tags(language: str) -> None:
    assert WorkspaceCreate(name="A", default_language=language).default_language == (
        language
    )


@pytest.mark.parametrize("language", ["", "e", "english language", "123", "en_US"])
def test_create_rejects_bad_language_tags(language: str) -> None:
    with pytest.raises(ValidationError):
        WorkspaceCreate(name="A", default_language=language)


def test_create_dedupes_platforms_keeping_order() -> None:
    data = WorkspaceCreate(
        name="A", default_platforms=["youtube", "facebook", "youtube"]
    )
    assert data.default_platforms == ["youtube", "facebook"]


def test_create_rejects_unknown_platform() -> None:
    with pytest.raises(ValidationError):
        WorkspaceCreate(name="A", default_platforms=["myspace"])  # type: ignore[list-item]


def test_create_rejects_oversized_settings() -> None:
    with pytest.raises(ValidationError):
        WorkspaceCreate(name="A", settings={"blob": "x" * 20_000})


@pytest.mark.parametrize("field", ["owner_id", "is_suspended", "id", "deleted_at"])
def test_create_and_update_reject_unknown_or_protected_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        WorkspaceCreate(name="A", **{field: "x"})
    with pytest.raises(ValidationError):
        WorkspaceUpdate(**{field: "x"})


def test_update_requires_at_least_one_field() -> None:
    with pytest.raises(ValidationError):
        WorkspaceUpdate()


@pytest.mark.parametrize("field", ["name", "default_language", "timezone"])
def test_update_forbids_null_for_required_columns(field: str) -> None:
    with pytest.raises(ValidationError):
        WorkspaceUpdate(**{field: None})


def test_update_allows_clearing_nullable_fields() -> None:
    data = WorkspaceUpdate(description=None, brand_voice=None, default_platforms=None)
    assert data.model_dump(exclude_unset=True) == {
        "description": None,
        "brand_voice": None,
        "default_platforms": None,
    }


def test_update_only_reports_sent_fields() -> None:
    data = WorkspaceUpdate(name="  New  ")
    assert data.model_dump(exclude_unset=True) == {"name": "New"}
