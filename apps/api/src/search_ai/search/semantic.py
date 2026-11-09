"""Vectorzoeken in pgvector met de gebruikers-ACL in de ``WHERE`` — dus vóór de afstands-ranking (B07-003).

Dit is de enige plek waar de vectorindex (tabel ``embedding``) wordt bevraagd. De functie krijgt een querievector die
al door een **lokaal** embeddingmodel is berekend. Zo'n model is een optionele app (standaard uit) en ontbreekt in P0;
daarom geeft ``GET /search/v1/search?mode=semantic`` nog eerlijk ``501 capability_unavailable`` en roepen in P0 alleen
tests deze functie aan (met vaste testvectoren). Er wordt nooit een vector of tekst naar buiten de NAS gestuurd.

Waarom de ACL hier niet ná de ranking mag: bij ``ORDER BY afstand LIMIT n`` zou een filter achteraf (a) resultaten van
andere gebruikers kunnen laten meetellen en (b) via aantallen/``has_more`` verraden dat er dichtbije vectoren van
anderen bestaan. Daarom staat ``acl.document_condition`` in dezelfde ``WHERE`` als het model-/dimensiefilter, en volgt
de join ``embedding → chunk → document_ref`` op **(id, source_id)**: de samengestelde sleutels uit migratie 0002 maken
een embedding met een andere bron dan zijn document onmogelijk.

De afstand is cosinus (``<=>``) op ``vector(dim)``; een HNSW-index per model moet dezelfde expressie gebruiken
(``(vector::vector(dim)) vector_cosine_ops``, partieel op model/versie/dimensie). Met zo'n index kan pgvector minder dan
``limit`` resultaten teruggeven (het filter na de indexscan); dat is veilig (nooit méér of andermans rijen).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Float, Select, and_, cast, literal, select
from sqlalchemy.orm import Session

from ..security.auth import Principal
from . import acl
from .models import Chunk, DocumentRef, Embedding
from .vector import Vector, to_pg

MAX_LIMIT = 100


@dataclass(frozen=True)
class VectorHit:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    source_id: uuid.UUID
    distance: float


def nearest_statement(
    principal: Principal, query: Sequence[float], *, model_name: str, model_version: str, limit: int = 20
) -> Select[Any]:
    """De query zelf (ook voor ``EXPLAIN`` in tests): ACL, model en dimensie in de WHERE, dan afstand en LIMIT."""
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit moet tussen 1 en {MAX_LIMIT} liggen")
    dim = len(query)
    qvec = cast(literal(to_pg(query)), Vector(dim))  # to_pg weigert lege, te lange en niet-eindige vectoren
    distance = cast(Embedding.vector, Vector(dim)).op("<=>", return_type=Float)(qvec)
    stmt = (
        select(Embedding.chunk_id, Chunk.document_id, DocumentRef.source_id, distance.label("afstand"))
        .join(Chunk, and_(Chunk.id == Embedding.chunk_id, Chunk.source_id == Embedding.source_id))
        .join(DocumentRef, and_(DocumentRef.id == Chunk.document_id, DocumentRef.source_id == Chunk.source_id))
        # ACL vóór ranking: alleen rijen die deze gebruiker mag zien komen bij ORDER BY afstand / LIMIT.
        .where(acl.document_condition(principal))
        .where(
            Embedding.model_name == model_name,
            Embedding.model_version == model_version,
            Embedding.dimension == dim,
        )
        # alleen op afstand sorteren (zoals pgvector aanraadt): zo kan een HNSW-index de volgorde leveren
        .order_by(distance)
        .limit(limit)
    )
    return stmt


def nearest_chunks(
    session: Session,
    principal: Principal,
    query: Sequence[float],
    *,
    model_name: str,
    model_version: str,
    limit: int = 20,
) -> list[VectorHit]:
    """De ``limit`` dichtstbijzijnde chunks die **deze gebruiker mag zien** (lege lijst zonder gebruikerscontext)."""
    if not principal.is_user:
        return []  # een service-token heeft geen gebruikerscontext (zie require_user_context)
    stmt = nearest_statement(principal, query, model_name=model_name, model_version=model_version, limit=limit)
    return [
        VectorHit(chunk_id=r.chunk_id, document_id=r.document_id, source_id=r.source_id, distance=float(r.afstand))
        for r in session.execute(stmt)
    ]
