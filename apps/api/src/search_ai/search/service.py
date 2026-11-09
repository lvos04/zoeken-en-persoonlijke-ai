"""B07-001 — bronselectie en toestemming: consent-register met scopes, lees-padenallowlist, gebruikers-ACL,
kinderprofiel-beperkingen en intrekking met gecontroleerde purge.

Toestandsmachine van een bron::

    draft ──consent──▶ active ──pause──▶ paused ──resume (zelfde momentopname)──▶ active
      │                  │                  │
      └──────────────────┴──── revoke ──────┴──▶ revoked  (afgeleide data verwijderd + gecontroleerd)

- ``draft``: geselecteerd maar zonder toestemming → er wordt niets gelezen, geïndexeerd of doorzocht.
- Toestemming is gebonden aan een momentopname (soort, paden, uitsluitingen, adres, scopes). Na het wijzigen van
  paden/uitsluitingen/adres (alleen in ``draft``/``paused``) is nieuwe toestemming nodig.
- Alleen de **gegevenseigenaar zelf** (``owner_sub``, geen kinderprofiel) geeft toestemming; een beheerder kan een bron
  voorbereiden maar niet namens een ander instemmen (ADR-0005). Intrekken kan de eigenaar én een beheerder.
- Intrekken verwijdert alle afgeleide data (documentverwijzingen, chunks, embeddings, transcriptverwijzingen),
  controleert dat er niets over is en legt dat vast als ``index_run`` (kind ``purge``). Originelen worden nooit
  aangeraakt: deze dienst heeft alleen leesrechten op de bronnen.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from .. import audit, events
from ..config import Settings
from ..errors import ApiError, capability_unavailable, conflict, forbidden
from ..security.auth import KNOWN_ROLES, Principal
from ..timeutil import iso, utcnow
from ..voice.models import ContextScope
from .models import (
    DB_SCOPES,
    AccessScope,
    Chunk,
    ConnectorPermission,
    DocumentRef,
    Embedding,
    IndexRun,
    IndexSource,
    PrivacyConsent,
    SearchQuery,
    TranscriptRef,
)
from .paths import PathGuard, PathRuleError, is_readonly_mount, validate_patterns, validate_read_roots
from .retention import keep_days

CONSENT_TEXT_VERSION = "b07-toestemming-1"
CONSENT_TEXT_NL = (
    "Ik geef toestemming om de geselecteerde bron lokaal op deze NAS te indexeren met precies de gekozen mappen, "
    "uitsluitingen en rechten (scopes). De originele bestanden worden alleen gelezen, nooit gewijzigd of verplaatst. "
    "De index (verwijzingen, tekstfragmenten en eventuele lokale embeddings) blijft op deze NAS en wordt niet naar "
    "een externe dienst gestuurd. Alleen de personen en rollen in de toegangslijst kunnen resultaten zien. Ik kan "
    "deze toestemming altijd intrekken; dan wordt alle indexdata van deze bron verwijderd."
)

SCOPE_TITLES_NL = {
    "metadata.read": "Namen, datums en metadata lezen",
    "content.read": "Tekstinhoud lezen voor de lokale index",
    "embeddings.local": "Lokale embeddings maken (lokaal model; optioneel en standaard uit)",
    "transcripts.read": "Bestaande ondertitels/transcripten gebruiken",
    "ai.external": "Passages naar een externe AI-dienst sturen",
}
SCOPES_PER_KIND: dict[str, tuple[str, ...]] = {
    "files": ("metadata.read", "content.read", "embeddings.local"),
    "logs": ("metadata.read", "content.read", "embeddings.local"),
    # Immich: alleen metadata (albums, datums, beschrijvingen); nooit gezichtsdata of de foto's zelf.
    "immich": ("metadata.read", "embeddings.local"),
    # Jellyfin: bibliotheekmetadata en bestaande ondertitels; geen bestanden muteren.
    "jellyfin": ("metadata.read", "transcripts.read", "embeddings.local"),
}
PATH_KINDS = ("files", "logs")
API_KINDS = ("immich", "jellyfin")
SCOPE_FIELDS = ("allowed_paths", "exclude_patterns", "base_url")


# ------------------------------------------------------------------ hulpfuncties
def read_roots(settings: Settings) -> tuple[str, ...]:
    roots, _warnings = validate_read_roots(settings.read_roots, settings.data_dir)
    return roots


def guard_for(settings: Settings, src: IndexSource) -> PathGuard:
    return PathGuard(read_roots(settings), tuple(src.allowed_paths or []), tuple(src.exclude_patterns or []))


def snapshot(src: IndexSource, scopes: list[str]) -> tuple[dict[str, Any], str]:
    snap = {
        "source_id": str(src.id),
        "kind": src.kind,
        "allowed_paths": sorted(src.allowed_paths or []),
        "exclude_patterns": sorted(src.exclude_patterns or []),
        "base_url": src.base_url,
        "scopes": sorted(scopes),
        "consent_text_version": CONSENT_TEXT_VERSION,
    }
    raw = json.dumps(snap, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return snap, hashlib.sha256(raw).hexdigest()


def active_consent(session: Session, source_id: uuid.UUID) -> PrivacyConsent | None:
    return session.scalars(
        select(PrivacyConsent).where(PrivacyConsent.source_id == source_id, PrivacyConsent.revoked_at.is_(None))
    ).first()


def active_scopes(session: Session, source_id: uuid.UUID) -> list[str]:
    return sorted(
        session.scalars(
            select(ConnectorPermission.scope).where(
                ConnectorPermission.source_id == source_id, ConnectorPermission.revoked_at.is_(None)
            )
        )
    )


def derived_counts(session: Session, source_id: uuid.UUID) -> dict[str, int]:
    def n(model: Any) -> int:
        return int(session.scalar(select(func.count()).select_from(model).where(model.source_id == source_id)) or 0)

    return {
        "documenten": n(DocumentRef),
        "chunks": n(Chunk),
        "embeddings": n(Embedding),
        "transcripten": n(TranscriptRef),
    }


def history_refs(session: Session, source_id: uuid.UUID) -> int:
    """Aantal bewaarde zoekopdrachten (alleen bij retentie > 0) met een resultaat uit deze bron."""
    return int(
        session.scalar(
            select(func.count()).select_from(SearchQuery).where(SearchQuery.source_ids.contains([str(source_id)]))
        )
        or 0
    )


def acl_entries(session: Session, source_id: uuid.UUID) -> list[AccessScope]:
    return list(
        session.scalars(
            select(AccessScope)
            .where(AccessScope.source_id == source_id, AccessScope.revoked_at.is_(None))
            .order_by(AccessScope.created_at)
        )
    )


def access_dict(a: AccessScope) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "principal_type": a.principal_type,
        "principal_id": a.principal_id,
        "permission": a.permission,
        "created_by": a.created_by,
        "created_at": iso(a.created_at),
    }


def consent_dict(c: PrivacyConsent | None) -> dict[str, Any] | None:
    if c is None:
        return None
    return {
        "id": str(c.id),
        "subject_sub": c.subject_sub,
        "scopes": list(c.scopes),
        "consent_text_version": c.consent_text_version,
        "snapshot_sha256": c.snapshot_sha256,
        "granted_by": c.granted_by,
        "granted_at": iso(c.granted_at),
        "expires_at": iso(c.expires_at),
    }


def source_view(session: Session, src: IndexSource, principal: Principal, settings: Settings) -> dict[str, Any]:
    """Volledige weergave voor beheerders; beperkte weergave (zonder paden/adres) voor andere gebruikers."""
    base: dict[str, Any] = {
        "id": str(src.id),
        "kind": src.kind,
        "name": src.name,
        "description": src.description,
        "status": src.status,
        "classification": src.classification,
        "kid_allowed": src.kid_allowed,
        "updated_at": iso(src.updated_at),
        "etag": f'W/"{src.version}"',
    }
    if not (principal.is_admin or principal.subject == src.owner_sub):
        return base
    consent = active_consent(session, src.id)
    current_sha = snapshot(src, list(consent.scopes) if consent else [])[1]
    base.update(
        {
            "owner_sub": src.owner_sub,
            "allowed_paths": list(src.allowed_paths or []),
            "exclude_patterns": list(src.exclude_patterns or []),
            "base_url": src.base_url,
            "credential_ref_present": src.credential_ref is not None,
            "version": src.version,
            "created_at": iso(src.created_at),
            "created_by": src.created_by,
            "updated_by": src.updated_by,
            "consent": consent_dict(consent),
            "consent_matches_current": consent is not None and consent.snapshot_sha256 == current_sha,
            "scopes": active_scopes(session, src.id),
            "available_scopes": list(SCOPES_PER_KIND[src.kind]),
            "access": [access_dict(a) for a in acl_entries(session, src.id)],
            "revoked_at": iso(src.revoked_at),
            "revoked_by": src.revoked_by,
            "purge_status": src.purge_status,
            "purge_verified_at": iso(src.purge_verified_at),
            "derived": derived_counts(session, src.id),
            "data_leaves_nas": False,
        }
    )
    return base


def _bump(src: IndexSource, principal: Principal) -> None:
    src.version += 1
    src.updated_by = principal.audit_id
    src.updated_at = utcnow()


def _path_error(exc: PathRuleError) -> ApiError:
    return ApiError(422, exc.code, str(exc), {"pad": exc.path} if exc.path else {})


def _clean_base_url(kind: str, url: str | None) -> str | None:
    if kind in PATH_KINDS:
        if url:
            raise ApiError(422, "base_url_niet_toegestaan", "Een bestandsbron heeft geen adres; gebruik mappen.")
        return None
    if not url:
        return None
    from ..hubclient import HubUrlError, validate_hub_url

    try:
        return validate_hub_url(url, allow_loopback=False, label="Het adres van de bron")
    except HubUrlError as exc:
        raise ApiError(422, "base_url_ongeldig", str(exc)) from exc


# ------------------------------------------------------------------ aanmaken en wijzigen
def create_source(
    session: Session, settings: Settings, principal: Principal, body: dict[str, Any], trace: str
) -> IndexSource:
    kind = body["kind"]
    paths: list[str] = []
    patterns: list[str] = []
    try:
        if kind in PATH_KINDS:
            paths = PathGuard.validate_source_paths(
                body.get("allowed_paths") or [], read_roots(settings), settings.data_dir
            )
            patterns = validate_patterns(body.get("exclude_patterns") or [])
        elif body.get("allowed_paths"):
            raise ApiError(422, "paden_niet_toegestaan", "Deze bronsoort werkt via een API, niet via mappen.")
    except PathRuleError as exc:
        raise _path_error(exc) from exc
    classification = body.get("classification") or "sensitive"
    kid_allowed = bool(body.get("kid_allowed", False))
    if kid_allowed and classification == "sensitive":
        raise ApiError(
            422,
            "kinderprofiel_niet_bij_gevoelig",
            "Een bron met classificatie 'gevoelig' kan niet voor kinderprofielen worden opengezet.",
        )
    src = IndexSource(
        id=uuid.uuid4(),
        kind=kind,
        name=body["name"].strip(),
        description=(body.get("description") or "").strip(),
        owner_sub=(body.get("owner_sub") or principal.subject).strip(),
        status="draft",
        classification=classification,
        kid_allowed=kid_allowed,
        allowed_paths=paths,
        exclude_patterns=patterns,
        base_url=_clean_base_url(kind, body.get("base_url")),
        config={},
        version=1,
        created_by=principal.audit_id,
        updated_by=principal.audit_id,
    )
    session.add(src)
    session.flush()
    # De gegevenseigenaar staat standaard in de ACL; verder niemand (ook de beheerder niet).
    session.add(
        AccessScope(source_id=src.id, principal_type="user", principal_id=src.owner_sub, created_by=principal.audit_id)
    )
    audit.record(
        session,
        domain="search",
        action="source.created",
        entity_type="index_source",
        entity_id=str(src.id),
        principal=principal,
        trace_id=trace,
        message_nl=f"Bron '{src.name}' ({kind}) aangemaakt als concept; zonder toestemming wordt niets geïndexeerd.",
        details={"kind": kind, "paden": len(paths), "eigenaar": src.owner_sub},
    )
    events.emit(
        session,
        event_type="search.source.created",
        entity_type="index_source",
        entity_id=str(src.id),
        actor_type=principal.actor_type,
        actor_id=principal.audit_id,
        trace_id=trace,
        payload={"kind": kind, "status": "draft"},
    )
    return src


def update_source(
    session: Session, settings: Settings, principal: Principal, src: IndexSource, body: dict[str, Any], trace: str
) -> dict[str, Any]:
    if src.status == "revoked":
        raise conflict("bron_ingetrokken", "Deze bron is ingetrokken; maak een nieuwe bron aan.")
    changes: dict[str, Any] = {}
    scope_change = any(f in body for f in SCOPE_FIELDS)
    if scope_change and src.status == "active":
        raise conflict(
            "bron_actief_eerst_pauzeren",
            "Pauzeer de bron eerst. Na het wijzigen van mappen, uitsluitingen of adres is nieuwe toestemming nodig.",
        )
    try:
        if "allowed_paths" in body:
            if src.kind not in PATH_KINDS:
                raise ApiError(422, "paden_niet_toegestaan", "Deze bronsoort werkt via een API, niet via mappen.")
            new_paths = PathGuard.validate_source_paths(
                body["allowed_paths"] or [], read_roots(settings), settings.data_dir
            )
            if new_paths != list(src.allowed_paths):
                changes["allowed_paths"] = {"oud": len(src.allowed_paths), "nieuw": len(new_paths)}
                src.allowed_paths = new_paths
        if "exclude_patterns" in body:
            if src.kind not in PATH_KINDS:
                raise ApiError(422, "patronen_niet_toegestaan", "Uitsluitpatronen gelden alleen voor mappen.")
            pats = validate_patterns(body["exclude_patterns"] or [])
            if pats != list(src.exclude_patterns):
                changes["exclude_patterns"] = {"aantal": len(pats)}
                src.exclude_patterns = pats
    except PathRuleError as exc:
        raise _path_error(exc) from exc
    if "base_url" in body:
        url = _clean_base_url(src.kind, body["base_url"])
        if url != src.base_url:
            changes["base_url"] = True
            src.base_url = url
    for f in ("name", "description"):
        if f in body and body[f] is not None and body[f].strip() != getattr(src, f):
            setattr(src, f, body[f].strip())
            changes[f] = True
    classification = body.get("classification", src.classification)
    kid_allowed = bool(body.get("kid_allowed", src.kid_allowed))
    if kid_allowed and classification == "sensitive":
        raise ApiError(
            422,
            "kinderprofiel_niet_bij_gevoelig",
            "Een bron met classificatie 'gevoelig' kan niet voor kinderprofielen worden opengezet.",
        )
    if classification != src.classification:
        changes["classification"] = classification
        src.classification = classification
    if kid_allowed != src.kid_allowed:
        changes["kid_allowed"] = kid_allowed
        src.kid_allowed = kid_allowed
    if not changes:
        return {"gewijzigd": False, "purge": None}
    _bump(src, principal)
    purge_info = None
    if "allowed_paths" in changes or "exclude_patterns" in changes:
        # Smallere selectie: afgeleide data van wat er nu buiten valt, direct verwijderen.
        run = purge_outside_selection(session, settings, principal, src, trace)
        purge_info = run_dict(run) if run is not None else None
    audit.record(
        session,
        domain="search",
        action="source.updated",
        entity_type="index_source",
        entity_id=str(src.id),
        principal=principal,
        trace_id=trace,
        message_nl=f"Bron '{src.name}' gewijzigd.",
        details={"wijzigingen": changes},
    )
    return {"gewijzigd": True, "wijzigingen": changes, "purge": purge_info}


# ------------------------------------------------------------------ toestemming
def _check_scopes(src: IndexSource, scopes: list[str]) -> list[str]:
    wanted = sorted(set(scopes))
    if not wanted:
        raise ApiError(422, "geen_scopes", "Kies minstens één recht (scope).")
    if "ai.external" in wanted:
        raise capability_unavailable(
            "ai.external",
            "Externe AI-verwerking is in deze versie niet beschikbaar: indexdata verlaat de NAS niet.",
        )
    unknown = [s for s in wanted if s not in DB_SCOPES]
    if unknown:
        raise ApiError(422, "onbekende_scope", "Onbekende scope.", {"scopes": unknown})
    not_for_kind = [s for s in wanted if s not in SCOPES_PER_KIND[src.kind]]
    if not_for_kind:
        raise ApiError(
            422,
            "scope_niet_voor_bronsoort",
            "Deze scope past niet bij deze bronsoort.",
            {"scopes": not_for_kind, "toegestaan": list(SCOPES_PER_KIND[src.kind])},
        )
    return wanted


def _check_prerequisites(settings: Settings, src: IndexSource) -> None:
    if src.kind in PATH_KINDS:
        if not read_roots(settings):
            raise conflict("geen_leeswortels", "Er zijn geen geldige leeswortels ingesteld.")
        if not src.allowed_paths:
            raise conflict("geen_paden", "Deze bron heeft nog geen mappen.")
        problems = []
        for p in src.allowed_paths:
            ro = is_readonly_mount(p)
            if ro is None:
                problems.append({"pad": p, "probleem": "niet_bereikbaar"})
            elif settings.require_readonly_mounts and not ro:
                problems.append({"pad": p, "probleem": "niet_alleen_lezen"})
        if problems:
            raise conflict(
                "bron_niet_klaar",
                "Niet elke map is bereikbaar als alleen-lezen koppeling. Koppel de share in de Custom App als Host Path "
                "met 'Read Only' aan onder /bronnen en probeer opnieuw.",
                {"problemen": problems},
            )
    elif not src.base_url:
        raise conflict("geen_adres", "Vul eerst het adres van de bron in.")


def grant_consent(
    session: Session,
    settings: Settings,
    principal: Principal,
    src: IndexSource,
    scopes: list[str],
    text_version: str,
    expires_at: datetime | None,
    trace: str,
) -> PrivacyConsent:
    if principal.is_kid:
        raise forbidden("kinderprofiel", "Een kinderprofiel kan geen toestemming geven.")
    if principal.subject != src.owner_sub:
        raise forbidden(
            "alleen_eigenaar",
            "Alleen de eigenaar van de gegevens kan toestemming geven; een beheerder kan dat niet namens een ander.",
        )
    if src.status == "revoked":
        raise conflict("bron_ingetrokken", "Deze bron is ingetrokken; maak een nieuwe bron aan.")
    if text_version != CONSENT_TEXT_VERSION:
        raise conflict(
            "toestemmingstekst_verouderd",
            "De toestemmingstekst is gewijzigd; lees de nieuwe tekst en bevestig opnieuw.",
            {"huidige_versie": CONSENT_TEXT_VERSION},
        )
    if expires_at is not None and expires_at <= utcnow():
        raise ApiError(422, "vervaldatum_verleden", "De vervaldatum moet in de toekomst liggen.")
    wanted = _check_scopes(src, scopes)
    _check_prerequisites(settings, src)
    now = utcnow()
    old = active_consent(session, src.id)
    if old is not None:
        old.revoked_at, old.revoked_by, old.revoke_reason = now, principal.audit_id, "vervangen door nieuwe toestemming"
        session.execute(
            update(ConnectorPermission)
            .where(ConnectorPermission.source_id == src.id, ConnectorPermission.revoked_at.is_(None))
            .values(revoked_at=now, revoked_by=principal.audit_id)
        )
        session.flush()
    snap, sha = snapshot(src, wanted)
    consent = PrivacyConsent(
        id=uuid.uuid4(),
        source_id=src.id,
        subject_sub=principal.subject,
        purpose="search_index",
        scopes=wanted,
        scope_snapshot=snap,
        snapshot_sha256=sha,
        consent_text_version=CONSENT_TEXT_VERSION,
        granted_by=principal.audit_id,
        granted_at=now,
        expires_at=expires_at,
    )
    session.add(consent)
    session.flush()
    for scope in wanted:
        session.add(
            ConnectorPermission(source_id=src.id, consent_id=consent.id, scope=scope, granted_by=principal.audit_id)
        )
    src.status = "active"
    _bump(src, principal)
    audit.record(
        session,
        domain="search",
        action="consent.granted",
        entity_type="index_source",
        entity_id=str(src.id),
        principal=principal,
        trace_id=trace,
        message_nl=f"Toestemming gegeven voor bron '{src.name}' met scopes {', '.join(wanted)}.",
        details={"consent_id": str(consent.id), "scopes": wanted, "snapshot_sha256": sha, "vervangt": bool(old)},
    )
    events.emit(
        session,
        event_type="search.consent.granted",
        entity_type="index_source",
        entity_id=str(src.id),
        actor_type="user",
        actor_id=principal.audit_id,
        trace_id=trace,
        payload={"scopes": wanted},
    )
    return consent


def pause(session: Session, principal: Principal, src: IndexSource, trace: str) -> None:
    if src.status != "active":
        raise conflict("bron_niet_actief", "Alleen een actieve bron kan worden gepauzeerd.")
    src.status = "paused"
    _bump(src, principal)
    audit.record(
        session,
        domain="search",
        action="source.paused",
        entity_type="index_source",
        entity_id=str(src.id),
        principal=principal,
        trace_id=trace,
        message_nl=f"Bron '{src.name}' gepauzeerd: niet meer doorzoekbaar en niet meer geïndexeerd.",
    )


def resume(session: Session, principal: Principal, src: IndexSource, trace: str) -> None:
    if src.status != "paused":
        raise conflict("bron_niet_gepauzeerd", "Alleen een gepauzeerde bron kan worden hervat.")
    consent = active_consent(session, src.id)
    if consent is None or (consent.expires_at is not None and consent.expires_at <= utcnow()):
        raise conflict("geen_toestemming", "Er is geen geldige toestemming; vraag de eigenaar opnieuw toestemming.")
    if consent.snapshot_sha256 != snapshot(src, list(consent.scopes))[1]:
        raise conflict(
            "toestemming_verouderd",
            "De bron is gewijzigd sinds de toestemming (mappen, uitsluitingen of adres). Nieuwe toestemming is nodig.",
        )
    src.status = "active"
    _bump(src, principal)
    audit.record(
        session,
        domain="search",
        action="source.resumed",
        entity_type="index_source",
        entity_id=str(src.id),
        principal=principal,
        trace_id=trace,
        message_nl=f"Bron '{src.name}' hervat.",
    )


SYSTEM_EXPIRY = Principal(kind="service", subject="worker-toestemming-verlopen", roles=frozenset())


def revoke(
    session: Session,
    settings: Settings,
    principal: Principal,
    src: IndexSource,
    reason: str,
    trace: str,
    *,
    system: bool = False,
) -> IndexRun:
    if not system and (principal.is_kid or not (principal.is_admin or principal.subject == src.owner_sub)):
        raise forbidden("geen_recht_intrekken", "Alleen de eigenaar van de gegevens of een beheerder kan intrekken.")
    now = utcnow()
    session.execute(
        update(PrivacyConsent)
        .where(PrivacyConsent.source_id == src.id, PrivacyConsent.revoked_at.is_(None))
        .values(revoked_at=now, revoked_by=principal.audit_id, revoke_reason=reason[:200] or "ingetrokken")
    )
    session.execute(
        update(ConnectorPermission)
        .where(ConnectorPermission.source_id == src.id, ConnectorPermission.revoked_at.is_(None))
        .values(revoked_at=now, revoked_by=principal.audit_id)
    )
    session.execute(
        update(AccessScope)
        .where(AccessScope.source_id == src.id, AccessScope.revoked_at.is_(None))
        .values(revoked_at=now, revoked_by=principal.audit_id)
    )
    # B15: een ingetrokken bron is ook geen spraak-/chatcontext meer
    session.execute(
        update(ContextScope)
        .where(ContextScope.search_source_id == src.id, ContextScope.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    already = src.status == "revoked"
    src.status = "revoked"
    src.revoked_at = src.revoked_at or now
    src.revoked_by = src.revoked_by or principal.audit_id
    _bump(src, principal)
    run = purge_source(session, principal, src, trace, reason="intrekking")
    audit.record(
        session,
        domain="search",
        action="consent.revoked",
        entity_type="index_source",
        entity_id=str(src.id),
        principal=principal,
        trace_id=trace,
        message_nl=(
            f"Toestemming voor bron '{src.name}' ingetrokken; afgeleide indexdata verwijderd "
            f"({'gecontroleerd' if run.status == 'succeeded' else 'controle mislukt, wordt opnieuw geprobeerd'})."
        ),
        details={"index_run": str(run.id), "al_ingetrokken": already, "reden": reason[:200]},
    )
    events.emit(
        session,
        event_type="search.source.revoked",
        entity_type="index_source",
        entity_id=str(src.id),
        actor_type="user" if principal.is_user else "system",
        actor_id=principal.audit_id,
        trace_id=trace,
        payload={"index_run": str(run.id), "purge_verified": run.status == "succeeded", "counts": run.counts},
    )
    return run


# ------------------------------------------------------------------ purge (opschonen)
def run_dict(run: IndexRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "source_id": str(run.source_id) if run.source_id else None,
        "kind": run.kind,
        "status": run.status,
        "started_at": iso(run.started_at),
        "finished_at": iso(run.finished_at),
        "counts": run.counts,
        "verification": run.verification,
        "error_code": run.error_code,
        "triggered_by": run.triggered_by,
        "trace_id": str(run.trace_id),
    }


def _new_run(principal: Principal | None, src: IndexSource, kind: str, trace: str) -> IndexRun:
    return IndexRun(
        id=uuid.uuid4(),
        source_id=src.id,
        kind=kind,
        status="running",
        started_at=utcnow(),
        triggered_by=principal.audit_id if principal else "system:worker",
        trace_id=uuid.UUID(trace),
    )


def purge_source(
    session: Session, principal: Principal | None, src: IndexSource, trace: str, reason: str = "opschonen"
) -> IndexRun:
    """Verwijdert alle afgeleide data van één bron en controleert dat er niets over is (bron blijft bestaan)."""
    run = _new_run(principal, src, "purge", trace)
    session.add(run)
    before = derived_counts(session, src.id)
    # Expliciet per tabel (naast ON DELETE CASCADE) zodat ook verweesde rijen met deze source_id verdwijnen.
    session.execute(delete(Embedding).where(Embedding.source_id == src.id))
    session.execute(delete(TranscriptRef).where(TranscriptRef.source_id == src.id))
    session.execute(delete(Chunk).where(Chunk.source_id == src.id))
    session.execute(delete(DocumentRef).where(DocumentRef.source_id == src.id))
    # Bewaarde zoekgeschiedenis (alleen bij retentie > 0) die naar deze bron verwijst, is ook afgeleide data: weg.
    # Alleen een ja/nee in het bewijs: een aantal zou de eigenaar vertellen hoe vaak anderen in zijn bron zochten.
    session.execute(delete(SearchQuery).where(SearchQuery.source_ids.contains([str(src.id)])))
    session.flush()
    after = derived_counts(session, src.id)
    history_left = history_refs(session, src.id)
    ok = all(v == 0 for v in after.values()) and history_left == 0
    run.status = "succeeded" if ok else "failed"
    run.finished_at = utcnow()
    run.counts = {"verwijderd": before, "reden": reason}
    run.verification = {
        "na_verwijderen": after,
        "zoekgeschiedenis_opgeschoond": history_left == 0,
        "leeg": ok,
        "gecontroleerd_op": iso(run.finished_at),
    }
    if not ok:
        run.error_code = "purge_onvolledig"
    src.purge_status = "done" if ok else "failed"
    src.purge_verified_at = run.finished_at if ok else None
    src.last_index_run_at = run.finished_at
    return run


def purge_outside_selection(
    session: Session, settings: Settings, principal: Principal | None, src: IndexSource, trace: str
) -> IndexRun | None:
    """Na een smallere selectie: verwijder afgeleide data van documenten die nu buiten de allowlist vallen."""
    if src.kind not in PATH_KINDS:
        return None
    guard = guard_for(settings, src)
    doomed = [
        d.id
        for d in session.scalars(select(DocumentRef).where(DocumentRef.source_id == src.id))
        if not guard.check(d.external_id)[0]
    ]
    if not doomed:
        return None
    run = _new_run(principal, src, "purge", trace)
    session.add(run)
    n_emb = session.execute(
        delete(Embedding).where(Embedding.chunk_id.in_(select(Chunk.id).where(Chunk.document_id.in_(doomed))))
    )
    n_chunks = session.execute(delete(Chunk).where(Chunk.document_id.in_(doomed)))
    n_tr = session.execute(delete(TranscriptRef).where(TranscriptRef.document_id.in_(doomed)))
    n_docs = session.execute(delete(DocumentRef).where(DocumentRef.id.in_(doomed)))
    session.flush()
    left = [
        d.external_id
        for d in session.scalars(select(DocumentRef).where(DocumentRef.source_id == src.id))
        if not guard.check(d.external_id)[0]
    ]
    run.status = "succeeded" if not left else "failed"
    run.finished_at = utcnow()
    run.counts = {
        "verwijderd": {
            "documenten": int(getattr(n_docs, "rowcount", 0) or 0),
            "chunks": int(getattr(n_chunks, "rowcount", 0) or 0),
            "embeddings": int(getattr(n_emb, "rowcount", 0) or 0),
            "transcripten": int(getattr(n_tr, "rowcount", 0) or 0),
        },
        "reden": "selectie_verkleind",
    }
    run.verification = {"buiten_selectie_over": len(left), "leeg": not left, "gecontroleerd_op": iso(run.finished_at)}
    return run


def expire_consents(session: Session, settings: Settings) -> int:
    """Worker: een verlopen toestemming wordt behandeld als intrekking (index en embeddings weg, gecontroleerd)."""
    now = utcnow()
    expired = list(
        session.scalars(
            select(PrivacyConsent).where(
                PrivacyConsent.purpose == "search_index",
                PrivacyConsent.revoked_at.is_(None),
                PrivacyConsent.expires_at.is_not(None),
                PrivacyConsent.expires_at <= now,
            )
        )
    )
    done = 0
    for consent in expired:
        src = session.get(IndexSource, consent.source_id) if consent.source_id else None
        if src is None:
            continue
        revoke(session, settings, SYSTEM_EXPIRY, src, "toestemming verlopen", str(uuid.uuid4()), system=True)
        done += 1
    return done


def sweep_revoked(session: Session) -> int:
    """Worker: purge opnieuw voor ingetrokken bronnen waar toch afgeleide data staat of de controle mislukte."""
    fixed = 0
    for src in session.scalars(select(IndexSource).where(IndexSource.status == "revoked")):
        clean = all(v == 0 for v in derived_counts(session, src.id).values()) and history_refs(session, src.id) == 0
        if src.purge_status == "done" and clean:
            continue
        purge_source(session, None, src, str(uuid.uuid4()), reason="nacontrole_intrekking")
        fixed += 1
    return fixed


# ------------------------------------------------------------------ ACL
def add_access(
    session: Session, principal: Principal, src: IndexSource, principal_type: str, principal_id: str, trace: str
) -> AccessScope:
    if src.status == "revoked":
        raise conflict("bron_ingetrokken", "Deze bron is ingetrokken.")
    principal_id = principal_id.strip()
    if principal_type == "role":
        if principal_id not in KNOWN_ROLES:
            raise ApiError(422, "onbekende_rol", "Onbekende rol.", {"rollen": sorted(KNOWN_ROLES)})
        if principal_id == "kind" and not src.kid_allowed:
            raise ApiError(
                422,
                "kinderprofiel_niet_toegestaan",
                "Zet eerst 'kinderprofielen toegestaan' aan voor deze bron (alleen bij niet-gevoelige bronnen).",
            )
    existing = session.scalars(
        select(AccessScope).where(
            AccessScope.source_id == src.id,
            AccessScope.principal_type == principal_type,
            AccessScope.principal_id == principal_id,
            AccessScope.revoked_at.is_(None),
        )
    ).first()
    if existing is not None:
        return existing
    entry = AccessScope(
        source_id=src.id, principal_type=principal_type, principal_id=principal_id, created_by=principal.audit_id
    )
    session.add(entry)
    session.flush()
    audit.record(
        session,
        domain="search",
        action="access.granted",
        entity_type="index_source",
        entity_id=str(src.id),
        principal=principal,
        trace_id=trace,
        message_nl=f"Leesrecht op bron '{src.name}' gegeven aan {principal_type} '{principal_id}'.",
        details={"access_id": str(entry.id), "type": principal_type, "wie": principal_id},
    )
    return entry


def remove_access(session: Session, principal: Principal, src: IndexSource, entry: AccessScope, trace: str) -> None:
    entry.revoked_at = utcnow()
    entry.revoked_by = principal.audit_id
    audit.record(
        session,
        domain="search",
        action="access.revoked",
        entity_type="index_source",
        entity_id=str(src.id),
        principal=principal,
        trace_id=trace,
        message_nl=f"Leesrecht op bron '{src.name}' ingetrokken voor {entry.principal_type} '{entry.principal_id}'.",
        details={"access_id": str(entry.id)},
    )


# ------------------------------------------------------------------ privacydekking
def coverage(session: Session, principal: Principal, settings: Settings) -> dict[str, Any]:
    roots, warnings = validate_read_roots(settings.read_roots, settings.data_dir)
    items = []
    q = select(IndexSource).order_by(IndexSource.created_at)
    if not principal.is_admin:
        q = q.where(IndexSource.owner_sub == principal.subject)
    for src in session.scalars(q):
        consent = active_consent(session, src.id)
        items.append(
            {
                "id": str(src.id),
                "name": src.name,
                "kind": src.kind,
                "status": src.status,
                "toestemming": consent is not None,
                "toestemming_sinds": iso(consent.granted_at) if consent else None,
                "scopes": active_scopes(session, src.id),
                "toegang": len(acl_entries(session, src.id)),
                "kinderprofielen": src.kid_allowed,
                "classificatie": src.classification,
                "afgeleide_data": derived_counts(session, src.id),
                "purge_status": src.purge_status,
                "verlaat_nas": False,
            }
        )
    return {
        "leeswortels": list(roots),
        "leeswortel_waarschuwingen": warnings if principal.is_admin else [],
        "externe_ai": "capability_unavailable",
        "ingest": "capability_unavailable",
        "zoekgeschiedenis_bewaren_dagen": keep_days(session, "search_query"),
        "items": items,
        "observed_at": iso(utcnow()),
    }
