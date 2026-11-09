"""Samenhang bron-id: afgeleide rijen horen altijd bij dezelfde bron als hun ouder (review P0).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-09

De gebruikers-ACL (B07-001/B07-003) filtert op ``document_ref.source_id``; de purge en de (toekomstige) vectorzoeker
werken ook met ``chunk.source_id`` en ``embedding.source_id``. Zonder samengestelde sleutel zou een fout in een
toekomstige ingest een chunk of embedding aan een andere bron kunnen hangen dan zijn document. Deze migratie maakt dat
onmogelijk in de database zelf:

- ``chunk(document_id, source_id)`` → ``document_ref(id, source_id)``
- ``transcript_ref(document_id, source_id)`` → ``document_ref(id, source_id)``
- ``embedding(chunk_id, source_id)`` → ``chunk(id, source_id)``

Rijen die daar nu al niet aan voldoen (alleen mogelijk door een fout; in P0 schrijft geen productiecode deze tabellen)
zijn ongeldige afgeleide indexdata en worden vooraf verwijderd; de index is altijd herbouwbaar uit de bronnen.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ongeldige (bron-verwisselde) afgeleide rijen opruimen vóór de nieuwe sleutels
    op.execute("DELETE FROM embedding e USING chunk c WHERE e.chunk_id = c.id AND e.source_id <> c.source_id")
    op.execute("DELETE FROM chunk c USING document_ref d WHERE c.document_id = d.id AND c.source_id <> d.source_id")
    op.execute(
        "DELETE FROM transcript_ref t USING document_ref d WHERE t.document_id = d.id AND t.source_id <> d.source_id"
    )
    op.create_unique_constraint(op.f("uq_document_ref_id_source_id"), "document_ref", ["id", "source_id"])
    op.create_unique_constraint(op.f("uq_chunk_id_source_id"), "chunk", ["id", "source_id"])

    op.drop_constraint(op.f("fk_embedding_chunk_id_chunk"), "embedding", type_="foreignkey")
    op.drop_constraint(op.f("fk_chunk_document_id_document_ref"), "chunk", type_="foreignkey")
    op.drop_constraint(op.f("fk_transcript_ref_document_id_document_ref"), "transcript_ref", type_="foreignkey")

    op.create_foreign_key(
        op.f("fk_chunk_document_id_document_ref"),
        "chunk",
        "document_ref",
        ["document_id", "source_id"],
        ["id", "source_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        op.f("fk_transcript_ref_document_id_document_ref"),
        "transcript_ref",
        "document_ref",
        ["document_id", "source_id"],
        ["id", "source_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        op.f("fk_embedding_chunk_id_chunk"),
        "embedding",
        "chunk",
        ["chunk_id", "source_id"],
        ["id", "source_id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_embedding_chunk_id_chunk"), "embedding", type_="foreignkey")
    op.drop_constraint(op.f("fk_transcript_ref_document_id_document_ref"), "transcript_ref", type_="foreignkey")
    op.drop_constraint(op.f("fk_chunk_document_id_document_ref"), "chunk", type_="foreignkey")
    op.create_foreign_key(
        op.f("fk_chunk_document_id_document_ref"),
        "chunk",
        "document_ref",
        ["document_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        op.f("fk_transcript_ref_document_id_document_ref"),
        "transcript_ref",
        "document_ref",
        ["document_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        op.f("fk_embedding_chunk_id_chunk"),
        "embedding",
        "chunk",
        ["chunk_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(op.f("uq_chunk_id_source_id"), "chunk", type_="unique")
    op.drop_constraint(op.f("uq_document_ref_id_source_id"), "document_ref", type_="unique")
