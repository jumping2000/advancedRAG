"""Request and response bodies of the knowledge bases API (REQ-008)."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

NAME_MAX_LENGTH = 100
DESCRIPTION_MAX_LENGTH = 500


def _name(raw: str) -> str:
    """The name without leading or trailing spaces, 1-100 characters (uniqueness is left to the database)."""
    name = raw.strip()
    if not 1 <= len(name) <= NAME_MAX_LENGTH:
        raise ValueError(f"the name must have 1 to {NAME_MAX_LENGTH} characters")
    return name


KbName = Annotated[str, AfterValidator(_name)]
Description = Annotated[str | None, Field(max_length=DESCRIPTION_MAX_LENGTH)]


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
