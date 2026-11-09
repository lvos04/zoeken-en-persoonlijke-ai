"""Replaycache voor Hub-JWT's (contract 1.2 §4.6).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-09

Elke mutatie met een Hub-JWT legt ``(iss, jti)`` vast tot ``exp + 30 s``; een tweede mutatie met dezelfde ``jti`` krijgt
401 ``token_replayed`` (behalve een exacte herhaling met dezelfde methode, hetzelfde pad en dezelfde
``Idempotency-Key``). De tabel is gedeeld door API en worker en overleeft herstarts. Bevat nooit een token.

De koppeling met de Hub verhuist in contract 1.2 uit de database naar ``/appdata/secrets/hub-link.json``. Dat gebeurt
**niet** in deze migratie maar bij de start van de dienst (``search_ai.enrollment.migrate_legacy_link``), want eerst moet
het bestand geschreven zijn en pas daarna mogen de oude rijen weg.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jwt_replay",
        sa.Column("iss", sa.String(length=40), nullable=False),
        sa.Column("jti", sa.UUID(), nullable=False),
        sa.Column("method", sa.String(length=10), nullable=False),
        sa.Column("path", sa.String(length=500), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("iss", "jti", name=op.f("pk_jwt_replay")),
    )
    op.create_index("ix_jwt_replay_expires_at", "jwt_replay", ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_jwt_replay_expires_at", table_name="jwt_replay")
    op.drop_table("jwt_replay")
