"""Request and response bodies of the knowledge bases API (REQ-008)."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, field_validator

NAME_MAX_LENGTH = 100
DESCRIPTION_MAX_LENGTH = 500


def _storable(text: str) -> str:
    """PostgreSQL text refuses NUL and lone surrogates: they get 422 here instead of a 500 from the database."""
    if "\x00" in text:
        raise ValueError("the text cannot contain NUL characters")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("the text is not valid Unicode") from None
    return text


def _name(raw: str) -> str:
    """The name without leading or trailing spaces, 1-100 characters (uniqueness is left to the database)."""
    name = _storable(raw).strip()
    if not 1 <= len(name) <= NAME_MAX_LENGTH:
        raise ValueError(f"the name must have 1 to {NAME_MAX_LENGTH} characters")
    return name


def _description(raw: str | None) -> str | None:
    """The description without leading or trailing spaces, at most 500 characters; a blank one is no description."""
    if raw is None:
        return None
    description = _storable(raw).strip()
    if len(description) > DESCRIPTION_MAX_LENGTH:
        raise ValueError(f"the description must have at most {DESCRIPTION_MAX_LENGTH} characters")
    return description or None


KbName = Annotated[str, AfterValidator(_name)]
Description = Annotated[str | None, AfterValidator(_description)]


class KnowledgeBaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime


class KnowledgeBaseCreateIn(BaseModel):
    # Only name and description: id, created_at or any other field of the body are ignored.
    name: KbName
    description: Description = None


class KnowledgeBaseUpdateIn(BaseModel):
    """Only the fields sent change; "description": null clears the description, a name cannot be null."""

    name: KbName | None = None
    description: Description = None

    @field_validator("name", mode="before")
    @classmethod
    def name_not_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("the name cannot be null")
        return value

    def changes(self) -> dict[str, Any]:
        return {field: getattr(self, field) for field in ("name", "description") if field in self.model_fields_set}
