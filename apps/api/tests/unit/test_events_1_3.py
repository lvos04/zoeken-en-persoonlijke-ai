"""Contract 1.3 §7 (C1.3-ZPA-05) zonder database: domeinen per eigenaar, de vaste ``service.*``-lijst, de grenzen van de
Hub-inbox vóór verzending en ``payload.ernst``."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from search_ai import APP_ID
from search_ai.events import (
    ERNST,
    SERVICE_EVENTS,
    check_event_type,
    emit,
    emit_service,
    envelope,
    validate_envelope,
)
from search_ai.models import OutboxEvent


class _Sessie:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)


def _env(ev: OutboxEvent, seq: int = 1) -> dict[str, Any]:
    ev.seq = seq
    return envelope(ev)


def test_vaste_service_lijst_van_1_3() -> None:
    assert {
        "service.hub_link.linked",
        "service.hub_link.unlinked",
        "service.hub_link.revoked",
        "service.token.rotated",
        "service.key_rotation.applied",
        "service.key_rotation.rejected",
    } == SERVICE_EVENTS
    assert ERNST == ("kritiek", "hoog", "middel", "laag", "info")


@pytest.mark.parametrize(
    "ok",
    [
        "search.source.created",
        "search.consent.granted",
        "voice.session.started",
        "app.health.changed",
        *sorted(SERVICE_EVENTS),
    ],
)
def test_toegestane_event_types(ok: str) -> None:
    check_event_type(ok)


@pytest.mark.parametrize(
    "fout",
    [
        "service.hub_link.paired",  # oude naam (1.2)
        "service.hub_key.rotation_rejected",  # oude naam (1.2)
        "service.enrollment.completed",  # oude naam (1.2)
        "media.download.failed",  # domein van een andere eigenaar
        "catalog.entry.created",
        "app.status.changed",  # gedeeld domein 'app': alleen app.health.changed
        "search.source",  # twee segmenten
        "search.source.created.extra",  # vier segmenten
        "Search.source.created",
        "search.source-x.created",
        "search." + "x" * 130 + ".created",
    ],
)
def test_verboden_event_types(fout: str) -> None:
    with pytest.raises(ValueError):
        check_event_type(fout)


def test_service_events_hebben_entiteit_service_en_de_eigen_app_id() -> None:
    sessie = _Sessie()
    ev = emit_service(
        sessie,  # type: ignore[arg-type]
        "service.hub_link.linked",
        actor_type="user",
        actor_id="statuspagina",
        trace_id=str(uuid.uuid4()),
        payload={"via": "koppelcode"},
    )
    env = _env(ev)
    assert env["entity"] == {"type": "service", "id": APP_ID} and env["payload"] == {"via": "koppelcode"}
    assert validate_envelope(env) == []
    with pytest.raises(ValueError):
        emit_service(sessie, "search.source.created", actor_type="user", actor_id="x", trace_id=str(uuid.uuid4()))  # type: ignore[arg-type]


def test_ernst_alleen_uit_de_vijf_woorden() -> None:
    sessie = _Sessie()
    for woord in ERNST:
        emit_service(
            sessie,  # type: ignore[arg-type]
            "service.hub_link.revoked",
            actor_type="system",
            actor_id="worker",
            trace_id=str(uuid.uuid4()),
            payload={"ernst": woord},
        )
    with pytest.raises(ValueError, match="ernst"):
        emit_service(
            sessie,  # type: ignore[arg-type]
            "service.hub_link.revoked",
            actor_type="system",
            actor_id="worker",
            trace_id=str(uuid.uuid4()),
            payload={"ernst": "warning"},
        )


def test_lange_actor_id_en_grote_payload_blijven_binnen_de_grenzen() -> None:
    sessie = _Sessie()
    ev = emit(
        sessie,  # type: ignore[arg-type]
        event_type="search.source.created",
        entity_type="index_source",
        entity_id=str(uuid.uuid4()),
        actor_type="user",
        actor_id="user:" + "a" * 200,
        trace_id=str(uuid.uuid4()),
        payload={f"k{i}": "x" * 100 for i in range(400)},  # ~40 KiB
    )
    env = _env(ev)
    assert len(env["actor"]["id"]) <= 128 and env["actor"]["id"].startswith("user:sha256:")
    assert env["payload"]["_ingekort"] is True
    assert validate_envelope(env) == []


@pytest.mark.parametrize(
    ("wijziging", "fout"),
    [
        (lambda e: e.update(onbekend=1), "onbekende"),
        (lambda e: e.update(event_type="search.source"), "event_type"),
        (lambda e: e.update(source_app="home-control-hub"), "source_app"),
        (lambda e: e.update(entity={"type": "Index Source", "id": "x"}), "entity.type"),
        (lambda e: e.update(entity={"type": "index_source", "id": ""}), "entity.id"),
        (lambda e: e.update(actor={"type": "user", "id": "x" * 129}), "actor.id"),
        (lambda e: e.update(timestamp_utc="2026-11-10T12:00:00"), "tijdzone"),
        (lambda e: e.update(payload={"blob": "x" * 17000}), "16 KiB"),
        (lambda e: e.update(payload={"ernst": "ernstig"}), "ernst"),
        (lambda e: e.update(source_event_id=""), "source_event_id"),
        (lambda e: e.update(schema_version="1.3"), "schema_version"),
    ],
)
def test_inboxgrenzen_voor_verzending(wijziging: Any, fout: str) -> None:
    ev = emit(
        _Sessie(),  # type: ignore[arg-type]
        event_type="search.source.created",
        entity_type="index_source",
        entity_id=str(uuid.uuid4()),
        actor_type="user",
        actor_id="user:beheer",
        trace_id=str(uuid.uuid4()),
        payload={"kind": "files"},
    )
    ev.timestamp_utc = datetime(2026, 11, 10, 12, tzinfo=UTC)
    env = _env(ev)
    assert validate_envelope(env) == []
    wijziging(env)
    problemen = validate_envelope(env)
    assert problemen and any(fout in p for p in problemen), problemen
