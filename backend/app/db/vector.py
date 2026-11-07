"""The pgvector `vector` column type, without the `pgvector` Python library (ADR-002)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.engine import Dialect
from sqlalchemy.types import UserDefinedType


class Vector(UserDefinedType[list[float]]):
    """`vector` with no fixed dimension. Python side: list[float]; database side: the text form `[x,y,...]`."""

    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "vector"

    def bind_processor(self, dialect: Dialect) -> Any:
        raise NotImplementedError

    def bind_expression(self, bindvalue: Any) -> Any:
        raise NotImplementedError

    def result_processor(self, dialect: Dialect, coltype: Any) -> Any:
        raise NotImplementedError
