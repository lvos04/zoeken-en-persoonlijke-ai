"""Dienst-E2E over echte HTTP (echte processen: API + worker van deze dienst, Hub-simulator in een eigen proces):
koppelen volgens contract 1.2 (env-token + vingerafdruk, pinning, bevestigen), B07-001 (bronselectie, toestemming,
ACL vóór ranking, intrekken), dienst-JWT, bodygrens, tokenrotatie, uitval van de Hub en intrekking (EX16).

De index wordt hier gevuld met een **simulatie van de toekomstige ingest** (B07-002 is nog niet gebouwd): de test schrijft
documentverwijzingen/chunks rechtstreeks in de database, zoals de ingest dat later doet. De dienst zelf leest in P0
nooit bestandsinhoud.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import stat
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
    assert _wait(lambda: httpx.get(f"{stack.api}/readyz").json()["hub_link"]["state"] == "gekoppeld")
    req = httpx.get(f"{stack.hub}/sim/enroll-requests").json()[0]
    assert set(req) == {"app_id", "version", "base_url", "public_key_jwk"}  # + het geheim (niet bewaard in de sim)
    assert req["app_id"] == "zoeken-en-persoonlijke-ai" and set(req["public_key_jwk"]) == {"kty", "crv", "x", "kid"}
    page = httpx.get(f"{stack.api}/").text
    assert "gekoppeld" in page and "niet gekoppeld" not in page and stack.fingerprint in page
    assert 'action="/koppelen"' not in page and "ontkoppelcode" not in page.lower()
    link = stack.data / "secrets" / "hub-link.json"
    assert stat.S_IMODE(link.stat().st_mode) == 0o600
    doc = json.loads(link.read_text())
    assert doc["pins"]["keyset_fingerprint"] == stack.fingerprint and doc["via"] == "token"
    assert _wait(lambda: json.loads(link.read_text())["confirmed_at"] is not None)
    paths = [r["path"] for r in httpx.get(f"{stack.hub}/sim/requests").json()]
    assert "/api/v1/services/self" in paths and "/api/v1/auth/jwks" not in paths  # nooit een JWKS opgehaald


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
        "/api/v1/services/self",
        "/api/v1/auth/key-rotations",
        "/api/v1/events/inbox",
    }


def test_03_connector_with_hub_service_jwt(stack: Stack) -> None:
    hdr = stack.service()
    d = httpx.get(f"{stack.api}/connector/v1/discover", headers=hdr).json()
    assert d["functions"]["search.ingest"] == "capability_unavailable"
    assert httpx.post(f"{stack.api}/connector/v1/apply", headers=hdr).status_code == 501
    assert httpx.get(f"{stack.api}/search/v1/search", params={"q": "x"}, headers=hdr).status_code == 403
    link = httpx.get(f"{stack.api}/connector/v1/hub-link", headers=hdr).json()
    assert link["state"] == "gekoppeld" and link["keyset_fingerprint"] == stack.fingerprint
    # het eigen service-token wordt inkomend nooit geaccepteerd (contract 1.2 §4.3)
    tok = httpx.get(f"{stack.hub}/sim/service-token").json()["token"]
    r = httpx.get(f"{stack.api}/connector/v1/discover", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401 and r.json()["details"]["reason"] == "service_token_not_accepted"
    # replay: dezelfde dienst-JWT voor een tweede, andere mutatie
    fixed = {"Authorization": hdr["Authorization"]}
    assert httpx.post(f"{stack.api}/connector/v1/propose", headers=fixed).status_code == 501
    r = httpx.post(f"{stack.api}/connector/v1/rollback", headers=fixed)
    assert r.status_code == 401 and r.json()["details"]["reason"] == "token_replayed"


def test_04_body_limit_over_real_http(stack: Stack) -> None:
    host, port = stack.api.removeprefix("http://").split(":")
    # chunked, zonder Content-Length, zonder Authorization: 413 vóór de authenticatie
    with socket.create_connection((host, int(port)), timeout=10) as sock:
        sock.sendall(
            b"POST /search/v1/sources HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n"
            b"Content-Type: application/json\r\n\r\n"
        )
        chunk = b"x" * 65536
        try:
            for _ in range(17):  # 17 × 64 KiB > 1 MiB
                sock.sendall(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
            sock.sendall(b"0\r\n\r\n")
        except OSError:
            pass  # de dienst mag de verbinding na het 413-antwoord sluiten
        data = b""
        while chunk_in := sock.recv(65536):
            data += chunk_in
        reply = data.decode(errors="replace")
    head, _, body = reply.partition("\r\n\r\n")
    assert head.startswith("HTTP/1.1 413")
    assert json.loads(body)["code"] == "payload_too_large" and json.loads(body)["details"] == {"max_bytes": 1048576}
    r = httpx.post(f"{stack.api}/search/v1/sources", content=b"x" * (1024 * 1024 + 1))
    assert r.status_code == 413 and r.json()["details"] == {"max_bytes": 1048576}


def test_05_token_rotation_requested_by_hub(stack: Stack) -> None:
    before = httpx.get(f"{stack.hub}/sim/service-token").json()
    httpx.post(f"{stack.hub}/sim/request-rotation")
    assert _wait(lambda: httpx.get(f"{stack.hub}/sim/service-token").json()["issued"] > before["issued"])
    link = stack.data / "secrets" / "hub-link.json"
    assert _wait(lambda: json.loads(link.read_text())["service_token"] != before["token"])
    assert _wait(lambda: json.loads(link.read_text())["confirmed_at"] is not None)
    assert httpx.get(f"{stack.api}/connector/v1/discover", headers=stack.token("viewer")).status_code == 200


def test_06_hub_outage_is_never_a_revocation(stack: Stack) -> None:
    # tokens vooraf halen (de simulator staat zo stil); lezen mag met dezelfde JWT vaker
    hdr = {"Authorization": stack.token("admin", sub="beheer")["Authorization"]}
    os.kill(stack.hub_proc.pid, signal.SIGSTOP)
    try:
        time.sleep(4)  # de worker controleert elke 2 s bij de Hub; die reageert nu niet
        r = httpx.get(f"{stack.api}/connector/v1/discover", headers=hdr)
        assert r.status_code == 200  # gepinde sleutels: geen Hub nodig om een JWT te controleren
    finally:
        os.kill(stack.hub_proc.pid, signal.SIGCONT)
    time.sleep(2)
    assert httpx.get(f"{stack.api}/readyz").json()["hub_link"]["state"] == "gekoppeld"
    assert httpx.get(f"{stack.api}/healthz").status_code == 200


def test_07_revocation_is_fail_closed(stack: Stack) -> None:
    hdr = stack.token("admin", sub="beheer")
    assert httpx.get(f"{stack.api}/connector/v1/discover", headers=hdr).status_code == 200
    httpx.post(f"{stack.hub}/sim/revoke")
    assert _wait(lambda: httpx.get(f"{stack.api}/readyz").json()["hub_link"]["state"] == "ingetrokken")
    r = httpx.get(f"{stack.api}/connector/v1/discover", headers=hdr)
    assert r.status_code == 401 and r.json()["details"]["reason"] == "revoked"
    page = httpx.get(f"{stack.api}/").text
    assert "ingetrokken" in page and 'action="/koppelen"' not in page
    assert httpx.get(f"{stack.api}/healthz").status_code == 200  # de dienst zelf blijft bereikbaar
