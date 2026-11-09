"""Gebruikers-ACL (B07-001/B07-003): wie mag resultaten uit welke bron zien — afgedwongen **vóór ranking**.

Elke zoekopdracht (full-text nu, vector/hybride later) gebruikt ``document_condition(principal)`` in de ``WHERE``.
Daardoor ziet de ranking (``ts_rank``/vectorafstand) alleen rijen die de gebruiker mag zien; er kan via scores,
aantallen of ``LIMIT`` niets uitlekken uit bronnen van anderen.

Regels (fail-closed):
1. De bron is ``active`` én heeft een actieve, niet-verlopen toestemming én minstens één actieve scope.
2. Er is een actieve ``access_scope`` voor de gebruiker (``sub``) of voor een van zijn rollen. Ook een beheerder ziet
   geen inhoud zonder ACL-regel: beheren ≠ meelezen (het toevoegen van zichzelf wordt geaudit).
3. Documenten met ``visibility = 'owner'`` zijn alleen voor hun eigenaar (bv. privéfoto's van één Immich-gebruiker).
4. **Kinderprofiel** (rol ``kind``, wint altijd van andere rollen): alleen bronnen met ``kid_allowed``, nooit
   ``sensitive``, en alleen documenten die expliciet ``kid_safe`` zijn.
5. Een service-token heeft geen gebruikerscontext en krijgt nooit inhoud (zie ``require_user_context``).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Select, and_, exists, false, or_, select
from sqlalchemy.sql.elements import ColumnElement

from ..security.auth import Principal
from ..timeutil import utcnow
from .models import ROLE_NAMES, AccessScope, ConnectorPermission, DocumentRef, IndexSource, PrivacyConsent


def source_is_active() -> ColumnElement[bool]:
    now = utcnow()
    consent_ok = exists(
        select(PrivacyConsent.id).where(
            PrivacyConsent.source_id == IndexSource.id,
            PrivacyConsent.revoked_at.is_(None),
            or_(PrivacyConsent.expires_at.is_(None), PrivacyConsent.expires_at > now),
        )
    )
    permission_ok = exists(
        select(ConnectorPermission.id).where(
            ConnectorPermission.source_id == IndexSource.id, ConnectorPermission.revoked_at.is_(None)
        )
    )
    return and_(IndexSource.status == "active", consent_ok, permission_ok)


def acl_grants(principal: Principal) -> ColumnElement[bool]:
    who: list[ColumnElement[bool]] = [
        and_(AccessScope.principal_type == "user", AccessScope.principal_id == principal.subject)
    ]
    roles = sorted(r for r in principal.roles if r in ROLE_NAMES)
    if roles:
        who.append(and_(AccessScope.principal_type == "role", AccessScope.principal_id.in_(roles)))
    return exists(
        select(AccessScope.id).where(
            AccessScope.source_id == IndexSource.id,
            AccessScope.revoked_at.is_(None),
            AccessScope.permission == "read",
            or_(*who),
        )
    )


def readable_sources(principal: Principal) -> Select[Any]:
    """``SELECT index_source.id`` van alle bronnen waarvan deze gebruiker resultaten mag zien."""
    if not principal.is_user:
        return select(IndexSource.id).where(false())
    conds: list[ColumnElement[bool]] = [source_is_active(), acl_grants(principal)]
    if principal.is_kid:
        conds += [IndexSource.kid_allowed.is_(True), IndexSource.classification != "sensitive"]
    return select(IndexSource.id).where(*conds)


def document_condition(principal: Principal) -> ColumnElement[bool]:
    """Voorwaarde op ``document_ref`` (te joinen met chunk/embedding) — gebruik in de WHERE vóór elke ranking."""
    if not principal.is_user:
        return false()
    visible = or_(DocumentRef.visibility == "source", DocumentRef.owner_sub == principal.subject)
    conds: list[ColumnElement[bool]] = [DocumentRef.source_id.in_(readable_sources(principal)), visible]
    if principal.is_kid:
        conds += [DocumentRef.kid_safe.is_(True), DocumentRef.classification != "sensitive"]
    return and_(*conds)


def can_view_source_admin(principal: Principal) -> bool:
    """Beheerweergave (paden, toestemming, ACL) van bronnen: alleen beheerders (nooit een kinderprofiel)."""
    return principal.is_user and principal.is_admin
