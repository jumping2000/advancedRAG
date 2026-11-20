"""Bodies of `GET` and `PUT /api/admin/settings` (REQ-004)."""

from typing import Self

from pydantic import BaseModel, StrictBool, model_validator


class StepState(BaseModel):
    """`enabled`: the saved switch; `configured`: the service of the step is set in `.env`."""

    enabled: bool
    configured: bool


class SettingsOut(BaseModel):
    rerank: StepState
    rewrite: StepState


class SettingsUpdate(BaseModel):
    """At least one of the two switches, each a JSON boolean: no coercion of strings or numbers ("yes", "true", 1),
    and `null` is not a value (422 `validation_error`)."""

    rerank_enabled: StrictBool | None = None
    rewrite_enabled: StrictBool | None = None

    @model_validator(mode="after")
    def _check_fields(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("rerank_enabled or rewrite_enabled is required")
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("rerank_enabled and rewrite_enabled must be booleans")
        return self
