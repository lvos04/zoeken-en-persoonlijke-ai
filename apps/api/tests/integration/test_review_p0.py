"""Zelfreview P0: regressietests voor de gevonden punten (auth op elke route, beheren ≠ meelezen in de bronwizard,
bronnamen niet voor elke viewer via de connector, verlopen toestemming = intrekking, verzoeklimiet, back-up vóór
upgrade met echte pg_dump)."""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update

from search_ai.config import Settings
from search_ai.db import Database
from search_ai.search.models import IndexSource, PrivacyConsent
from search_ai.search.service import CONSENT_TEXT_VERSION, expire_consents
from search_ai.timeutil import utcnow
from tests.conftest import Shares, idem, recreate_database
from tests.integration.helpers import counts, simulate_ingest

pytestmark = pytest.mark.integration

Auth = Callable[..., dict[str, str]]
PUBLIC = {"/", "/healthz", "/readyz", "/koppelen", "/ontkoppelen"}


def test_every_api_route_requires_auth(client: Any) -> None:
    checked = 0
    for route_path, ops in client.app.openapi()["paths"].items():
        if route_path in PUBLIC:
            continue
        path = route_path
        for param in ("source_id", "access_id", "evidence_id"):
            path = path.replace("{" + param + "}", str(uuid.uuid4()))
        for method in ops:
            r = client.request(method.upper(), path, params={"q": "x", "type": "search.source"}, json={})
            assert r.status_code == 401, (method, route_path, r.status_code, r.text)
            assert r.json()["code"] == "unauthorized"
            checked += 1
    assert checked >= 28


def _source(client: Any, auth: Auth, path: str, owner: str = "user-a") -> dict[str, Any]:
    r = client.post(
        "/search/v1/sources",
        json={"kind": "files", "name": "Docs", "allowed_paths": [path], "owner_sub": owner},
        headers=idem(auth("admin", sub="beheer")),
    )
    assert r.status_code == 201, r.text
    return dict(r.json())


def test_preview_shows_names_only_to_owner(paired_client: Any, auth: Auth, shares: Shares) -> None:
    src = _source(paired_client, auth, str(shares.a))
    admin = paired_client.get(f"/search/v1/sources/{src['id']}/preview", headers=auth("admin", sub="beheer")).json()
    assert admin["bestanden"] == 2 and admin["voorbeeld"] == [] and admin["voorbeeld_verborgen"] is True
    owner = paired_client.get(f"/search/v1/sources/{src['id']}/preview", headers=auth("viewer", sub="user-a")).json()
    assert len(owner["voorbeeld"]) == 2 and owner["voorbeeld_verborgen"] is False
    other = paired_client.get(f"/search/v1/sources/{src['id']}/preview", headers=auth("viewer", sub="user-b"))
    assert other.status_code == 404
    kid_owner = paired_client.get(f"/search/v1/sources/{src['id']}/preview", headers=auth("kind", sub="user-a"))
    assert kid_owner.status_code == 404


def test_connector_source_names_not_for_plain_viewer(paired_client: Any, auth: Auth) -> None:
    r = paired_client.get("/connector/v1/items", params={"type": "search.source"}, headers=auth("viewer"))
    assert r.status_code == 403
    r = paired_client.get("/connector/v1/items", params={"type": "search.source"}, headers=auth("operator"))
    assert r.status_code == 200


def test_expired_consent_is_treated_as_revocation(paired_client: Any, auth: Auth, shares: Shares) -> None:
    client = paired_client
    src = _source(client, auth, str(shares.a))
    r = client.post(
        f"/search/v1/sources/{src['id']}/consent",
        json={
            "scopes": ["metadata.read", "content.read"],
            "consent_text_version": CONSENT_TEXT_VERSION,
            "bevestiging": True,
            "expires_at": (utcnow() + timedelta(days=1)).isoformat(),
        },
        headers=idem(auth("viewer", sub="user-a")),
    )
    assert r.status_code == 200, r.text
    with client.app.state.db.sessions() as s:
        simulate_ingest(s, src["id"], str(shares.a / "notities.md"), ["vervalt"])
        s.execute(update(PrivacyConsent).values(expires_at=utcnow() - timedelta(seconds=1)))
        s.commit()
    # zoeken sluit verlopen toestemming direct uit …
    res = client.get("/search/v1/search", params={"q": "vervalt"}, headers=auth("viewer", sub="user-a")).json()
    assert res["items"] == []
    # … en de worker behandelt het als intrekking: alle afgeleide data weg, gecontroleerd
    with client.app.state.db.sessions() as s:
        assert expire_consents(s, client.app.state.settings) == 1
        s.commit()
        row = s.get(IndexSource, uuid.UUID(src["id"]))
        assert row is not None and row.status == "revoked" and row.purge_status == "done"
        assert counts(s, src["id"]) == {"documenten": 0, "chunks": 0, "embeddings": 0, "transcripten": 0}
        assert expire_consents(s, client.app.state.settings) == 0


def test_request_size_limit(paired_client: Any, auth: Auth) -> None:
    big = "x" * (1024 * 1024 + 10)
    r = paired_client.post(
        "/search/v1/sources",
        content=big,
        headers=idem(auth("admin"), **{"Content-Type": "application/json"}),
    )
    assert r.status_code == 413 and r.json()["code"] == "payload_too_large"


@pytest.mark.skipif(shutil.which("pg_dump") is None, reason="pg_dump niet beschikbaar")
def _use_client_tools_of_server_version(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Gebruik pg_dump én pg_restore van dezelfde hoofdversie als de server (zoals in de image: alleen PG 16).

    Op een ontwikkelmachine met meerdere PostgreSQL-clients kiest Debians pg_wrapper anders voor pg_dump mét host de
    nieuwste versie en voor pg_restore zonder host de standaardversie; dan kent pg_restore het dumpformaat niet.
    """
    from sqlalchemy import text

    db = Database(url)
    try:
        with db.sessions() as s:
            major = int(s.execute(text("SHOW server_version_num")).scalar_one()) // 10000
    finally:
        db.dispose()
    bindir = Path(f"/usr/lib/postgresql/{major}/bin")
    if (bindir / "pg_dump").exists() and (bindir / "pg_restore").exists():
        monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")


def test_pre_upgrade_backup_with_real_pg_dump(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Een openstaande migratie (testrevisie bovenop de echte head) → pg_dump vooraf; de nieuwste 3 blijven bewaard.

    Review P0: de back-up bevat bronnen, toestemming en ACL, maar **niet** de inhoud van de afgeleide index, de
    zoekgeschiedenis of spraaktranscripten — anders zou een ingetrokken bron in /appdata/backups blijven bestaan.
    """
    from sqlalchemy import text

    from search_ai.migrations import BACKUP_SCHEMA_ONLY_TABLES, alembic_dir, head_revision, pre_upgrade_backup, upgrade

    url = recreate_database("zoek_test_backup")
    _use_client_tools_of_server_version(url, monkeypatch)
    base = Settings(database_url=url)
    upgrade(base)
    head = head_revision(base)
    db = Database(url)
    try:
        with db.sessions() as s:
            src = IndexSource(
                kind="files",
                name="back-uptest",
                owner_sub="u",
                created_by="t",
                updated_by="t",
                allowed_paths=["/bronnen/x"],
            )
            s.add(src)
            s.flush()
            simulate_ingest(s, src.id, "/bronnen/x/geheim.txt", ["GEHEIME-INHOUD-UIT-DE-INDEX"])
            s.execute(
                text(
                    "INSERT INTO search_query (id, principal_sub, roles, query_text, mode, filters, result_count, "
                    "source_ids, created_at, expires_at) VALUES (gen_random_uuid(), 'u', '[]', 'GEHEIME-ZOEKVRAAG', "
                    "'fulltext', '{}', 1, '[]', now(), now() + interval '1 day')"
                )
            )
            s.commit()
    finally:
        db.dispose()
    mig = tmp_path / "alembic"
    shutil.copytree(alembic_dir(base), mig, ignore=shutil.ignore_patterns("__pycache__"))
    (mig / "versions" / "9001_test.py").write_text(
        f'"""testrevisie"""\nrevision = "9001"\ndown_revision = "{head}"\nbranch_labels = None\n'
        "depends_on = None\n\n\ndef upgrade() -> None:\n    pass\n\n\ndef downgrade() -> None:\n    pass\n",
        encoding="utf-8",
    )
    settings = Settings(database_url=url, alembic_dir=mig)
    db = Database(url)
    backups = tmp_path / "backups"
    try:
        dumps = [pre_upgrade_backup(settings, db, backups) for _ in range(4)]
    finally:
        db.dispose()
    assert all(d is not None for d in dumps)
    kept = sorted(backups.glob(f"voor-upgrade-{head}-*.dump"))
    assert 1 <= len(kept) <= 3
    listing = subprocess.run(["pg_restore", "--list", str(kept[-1])], capture_output=True, text=True, check=True)
    data_entries = {line.split()[-2] for line in listing.stdout.splitlines() if " TABLE DATA public " in line}
    # structuur van alle tabellen staat erin; inhoud van bronnen/toestemming/ACL wel, van de afgeleide index niet
    assert "TABLE public embedding" in listing.stdout and "TABLE public chunk" in listing.stdout
    assert {"index_source", "privacy_consent", "access_scope", "audit_event"} <= data_entries
    assert not data_entries & set(BACKUP_SCHEMA_ONLY_TABLES)
    sql = subprocess.run(["pg_restore", "-f", "-", str(kept[-1])], capture_output=True, text=True, check=True).stdout
    assert "back-uptest" in sql  # de bron zelf (configuratie) is bewaard …
    assert "GEHEIME-INHOUD-UIT-DE-INDEX" not in sql and "GEHEIME-ZOEKVRAAG" not in sql  # … de indexinhoud niet
    # zonder openstaande migratie: geen dump
    db = Database(url)
    try:
        assert pre_upgrade_backup(base, db, backups) is None
    finally:
        db.dispose()
