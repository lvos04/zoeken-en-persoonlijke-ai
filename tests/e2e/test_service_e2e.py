"""Dienst-E2E over echte HTTP: koppelen, B07-001 (bronselectie, toestemming, ACL vóór ranking, intrekken) en
fail-closed bij uitval van de Hub.

De index wordt hier gevuld met een **simulatie van de toekomstige ingest** (B07-002 is nog niet gebouwd): de test schrijft
documentverwijzingen/chunks rechtstreeks in de database, zoals de ingest dat later doet. De dienst zelf leest in P0
nooit bestandsinhoud.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from e2e.conftest import Stack

CONSENT_TEXT_VERSION = "b07-toestemming-1"


def _checksums(stack: Stack) -> dict[str, str]:
    return {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(stack.shares.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def _simulate_ingest(stack: Stack, source_id: str, external_id: str, text: str) -> None:
    from search_ai.search.models import Chunk, DocumentRef, Embedding

    eng = create_engine(stack.db_url)
    with Session(eng) as s:
        doc = DocumentRef(
            source_id=uuid.UUID(source_id),
            external_id=external_id,
            title=external_id.rsplit("/", 1)[-1],
        )
        s.add(doc)
        s.flush()
        ch = Chunk(
            document_id=doc.id,
            source_id=doc.source_id,
            ordinal=0,
            text=text,
            text_sha256=hashlib.sha256(text.encode()).hexdigest(),
        )
        s.add(ch)
        s.flush()
        s.add(
            Embedding(
                chunk_id=ch.id,
                source_id=doc.source_id,
                model_name="sim",
                model_version="0",
                dimension=2,
                vector=[1, 0],
            )
        )
        s.commit()
    eng.dispose()


def _wait(pred: object, timeout: float = 20) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():  # type: ignore[operator]
            return True
        time.sleep(0.5)
    return False


def test_01_ready_and_enrolled_with_one_time_token(stack: Stack) -> None:
    assert httpx.get(f"{stack.api}/readyz").status_code == 200
    assert _wait(
        lambda: (
            "gekoppeld" in httpx.get(f"{stack.api}/").text and "niet gekoppeld" not in httpx.get(f"{stack.api}/").text
        )
    )
    req = httpx.get(f"{stack.hub}/sim/enroll-requests").json()[0]
    assert req["app_id"] == "zoeken-en-persoonlijke-ai" and set(req) <= {
        "app_id",
        "version",
        "public_key_jwk",
        "base_url",
    }


def test_02_b07_001_flow_over_http(stack: Stack) -> None:
    admin = stack.token("admin", sub="beheer")
    share_a = str(stack.shares / "documenten")
    share_b = str(stack.shares / "prive-b")
    before = _checksums(stack)

    r = httpx.post(
        f"{stack.api}/search/v1/sources",
        json={
            "kind": "files",
            "name": "Documenten A",
            "allowed_paths": [share_a],
            "owner_sub": "user-a",
        },
        headers=stack.idem(admin),
    )
    assert r.status_code == 201, r.text
    a = r.json()
    r = httpx.post(
        f"{stack.api}/search/v1/sources",
        json={
            "kind": "files",
            "name": "Privé B",
            "allowed_paths": [share_b],
            "owner_sub": "user-b",
        },
        headers=stack.idem(admin),
    )
    b = r.json()

    pv = httpx.get(
        f"{stack.api}/search/v1/sources/{a['id']}/preview", headers=stack.token("viewer", sub="user-a")
    ).json()
    assert sorted(pv["voorbeeld"]) == [
        "documenten/notities.md",
        "documenten/werk/offerte.txt",
    ]
    assert pv["overgeslagen"]["symlink"] == 1 and pv["inhoud_gelezen"] is False

    # Alleen A krijgt toestemming (door de eigenaar zelf); B blijft concept.
    r = httpx.post(
        f"{stack.api}/search/v1/sources/{a['id']}/consent",
        json={
            "scopes": ["metadata.read", "content.read"],
            "consent_text_version": CONSENT_TEXT_VERSION,
            "bevestiging": True,
        },
        headers=stack.idem(stack.token("viewer", sub="user-a")),
    )
    assert r.status_code == 200, r.text
    assert (
        httpx.post(
            f"{stack.api}/search/v1/reindex",
            json={"source_id": b["id"]},
            headers=stack.idem(admin),
        ).status_code
        == 409
    )

    _simulate_ingest(stack, a["id"], share_a + "/werk/offerte.txt", "offerte voor de keuken")
    _simulate_ingest(stack, b["id"], share_b + "/dagboek.txt", "keuken keuken geheim dagboek")

    res = httpx.get(
        f"{stack.api}/search/v1/search",
        params={"q": "keuken"},
        headers=stack.token("viewer", sub="user-a"),
    )
    assert [i["bron"]["id"] for i in res.json()["items"]] == [a["id"]]
    res_b = httpx.get(
        f"{stack.api}/search/v1/search",
        params={"q": "keuken"},
        headers=stack.token("viewer", sub="user-b"),
    )
    assert res_b.json()["items"] == []  # B's eigen bron heeft geen toestemming → ook voor B onzichtbaar

    key = str(uuid.uuid4())
    r = httpx.post(
        f"{stack.api}/search/v1/sources/{a['id']}/revoke",
        json={"reden": "e2e"},
        headers=stack.idem(stack.token("viewer", sub="user-a"), key),
    )
    assert r.status_code == 200, r.text
    assert r.json()["opschonen"]["verification"]["leeg"] is True
    replay = httpx.post(
        f"{stack.api}/search/v1/sources/{a['id']}/revoke",
        json={"reden": "e2e"},
        headers=stack.idem(stack.token("viewer", sub="user-a"), key),
    )
    assert replay.headers.get("Idempotent-Replay") == "true" and replay.json() == r.json()
    res = httpx.get(
        f"{stack.api}/search/v1/search",
        params={"q": "keuken"},
        headers=stack.token("viewer", sub="user-a"),
    )
    assert res.json()["items"] == []
    assert _checksums(stack) == before

    # Events bereiken de Hub-inbox, zonder bestandsnamen of inhoud; verder is niets naar buiten gegaan.
    assert _wait(
        lambda: any(e["event_type"] == "search.source.revoked" for e in httpx.get(f"{stack.hub}/sim/inbox").json())
    )
    wire = json.dumps(httpx.get(f"{stack.hub}/sim/requests").json())
    for needle in ("offerte", "dagboek", "keuken", str(stack.shares)):
        assert needle not in wire
    paths = {r["path"] for r in httpx.get(f"{stack.hub}/sim/requests").json()}
    assert paths <= {
        "/api/v1/services/enroll",
        "/api/v1/auth/jwks",
        "/api/v1/events/inbox",
    }


def test_03_connector_with_service_token(stack: Stack) -> None:
    tok = httpx.get(f"{stack.hub}/sim/service-token").json()["token"]
    hdr = {"Authorization": f"Bearer {tok}"}
    d = httpx.get(f"{stack.api}/connector/v1/discover", headers=hdr).json()
    assert d["functions"]["search.ingest"] == "capability_unavailable"
    assert httpx.post(f"{stack.api}/connector/v1/apply", headers=hdr).status_code == 501
    assert httpx.get(f"{stack.api}/search/v1/search", params={"q": "x"}, headers=hdr).status_code == 403


def test_04_fail_closed_when_hub_disappears(stack: Stack) -> None:
    hdr = stack.token("admin", sub="beheer")
    assert httpx.get(f"{stack.api}/connector/v1/discover", headers=hdr).status_code == 200
    stack.hub_proc.terminate()
    stack.hub_proc.wait(10)
    time.sleep(5)  # ttl (2 s) + max_stale (2 s) verstreken
    r = httpx.get(f"{stack.api}/connector/v1/discover", headers=hdr)
    assert r.status_code == 401 and r.json()["details"]["reason"] == "jwks_niet_beschikbaar"
    assert httpx.get(f"{stack.api}/healthz").status_code == 200  # dienst zelf blijft bereikbaar
