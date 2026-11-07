"""The pgvector `vector` column type, without the `pgvector` Python library (ADR-002).

The value travels as the text `[x,y,...]`: asyncpg has no codec for `vector`, so the parameter is a string cast to
`vector` in the statement, and the column is read back as text and parsed.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import cast
from sqlalchemy.engine import Dialect
from sqlalchemy.types import UserDefinedType


class Vector(UserDefinedType[list[float]]):
    """`vector` with no fixed dimension. Python side: list[float]; database side: the text form `[x,y,...]`."""

    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "vector"

    def bind_processor(self, dialect: Dialect) -> Callable[[Sequence[float] | None], str | None]:
        def process(value: Sequence[float] | None) -> str | None:
            if value is None:
                return None
            numbers = [float(number) for number in value]
            if not all(math.isfinite(number) for number in numbers):
                raise ValueError("a vector holds only finite numbers (no NaN or infinity)")
            return "[" + ",".join(repr(number) for number in numbers) + "]"

        return process

    def bind_expression(self, bindvalue: Any) -> Any:
        return cast(bindvalue, self)

    def result_processor(self, dialect: Dialect, coltype: Any) -> Callable[[str | None], list[float] | None]:
        def process(value: str | None) -> list[float] | None:
            if value is None:
                return None
            body = value.strip()[1:-1].strip()
            return [float(number) for number in body.split(",")] if body else []

        return process
