"""SQLAlchemy-type voor pgvector's ``vector`` zonder extra (numpy-)afhankelijkheid.

Waarden zijn ``list[float]``; in de database ``vector`` (zonder vaste dimensie: het model en de dimensie staan per rij
in ``embedding.model_name``/``model_version``/``dimension``, afgedwongen met ``vector_dims(vector) = dimension``).
Een HNSW-/IVFFlat-index vereist een vaste dimensie en wordt in B07-003 per model als partiële index aangemaakt.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import cast
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.types import UserDefinedType

MAX_DIMENSION = 16000  # bovengrens van pgvector voor het type 'vector'


def to_pg(values: Sequence[float]) -> str:
    out: list[str] = []
    for v in values:
        f = float(v)
        if not math.isfinite(f):
            raise ValueError("vector bevat NaN of oneindig")
        out.append(repr(f))
    if not out:
        raise ValueError("vector mag niet leeg zijn")
    if len(out) > MAX_DIMENSION:
        raise ValueError(f"vector heeft meer dan {MAX_DIMENSION} dimensies")
    return "[" + ",".join(out) + "]"


def from_pg(raw: str) -> list[float]:
    body = raw.strip()
    if not (body.startswith("[") and body.endswith("]")):
        raise ValueError("geen pgvector-tekstwaarde")
    inner = body[1:-1].strip()
    return [float(x) for x in inner.split(",")] if inner else []


class Vector(UserDefinedType[list[float]]):
    cache_ok = True

    def __init__(self, dim: int | None = None) -> None:
        self.dim = dim

    def get_col_spec(self, **_kw: Any) -> str:
        return "VECTOR" if self.dim is None else f"VECTOR({int(self.dim)})"

    def bind_expression(self, bindvalue: Any) -> ColumnElement[Any]:
        return cast(bindvalue, self)

    def bind_processor(self, dialect: Any) -> Callable[[Any], str | None]:
        def process(value: Any) -> str | None:
            return None if value is None else to_pg(value)

        return process

    def result_processor(self, dialect: Any, coltype: Any) -> Callable[[Any], list[float] | None]:
        def process(value: Any) -> list[float] | None:
            if value is None:
                return None
            if isinstance(value, list | tuple):
                return [float(x) for x in value]
            return from_pg(str(value))

        return process


def _register_reflection() -> None:
    """Laat reflectie (Alembic-drifttest) het type 'vector' herkennen i.p.v. een waarschuwing te geven."""
    from sqlalchemy.dialects.postgresql.base import ischema_names

    ischema_names.setdefault("vector", Vector)


_register_reflection()
