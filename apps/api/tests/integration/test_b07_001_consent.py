"""B07-001 — Bronselectie en toestemming (integratie met PostgreSQL + pgvector).

Acceptatie (Excel): *Niet geselecteerde share indexeert niets en verlaat NAS niet.*
Daarnaast: consent-register met scopes, lees-padenallowlist, gebruikers-ACL vóór ranking, kinderprofiel-beperkingen
en intrekking die index/embeddings verwijdert (originelen ongemoeid).
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import select

from search_ai.search.models import AccessScope, ConnectorPermission, IndexRun, IndexSource, PrivacyConsent
from search_ai.search.service import CONSENT_TEXT_VERSION, sweep_revoked
from tests.conftest import FakeHub, Shares, idem
from tests.integration.helpers import counts, simulate_ingest

pytestmark = pytest.mark.integration

Auth = Callable[..., dict[str, str]]


def make_source(client: Any, auth: Auth, path: str, *, owner: str = "user-a", **extra: Any) -> dict[str, Any]:
    body = {"kind": "files", "name": extra.pop("name", "Documenten"), "allowed_paths": [path], "owner_sub": owner}
    body.update(extra)
    r = client.post("/search/v1/sources", json=body, headers=idem(auth("admin", sub="beheer")))
    assert r.status_code == 201, r.text
    return r.json()


def consent(client: Any, auth: Auth, sid: str, *, sub: str = "user-a", scopes: list[str] | None = None, **kw: Any) -> Any:
    body = {
        "scopes": scopes or ["metadata.read", "content.read"],
        "consent_text_version": CONSENT_TEXT_VERSION,
        "bevestiging": True,
    }
    return client.post(
        f"/search/v1/sources/{sid}/consent", json=body, headers=idem(auth(*kw.get("roles", ("viewer",)), sub=sub))
    )


def search(client: Any, auth: Auth, q: str, sub: str, *roles: str) -> dict[str, Any]:
    r = client.get("/search/v1/search", params={"q": q}, headers=auth(*(roles or ("viewer",)), sub=sub))
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------ acceptatie: niet-geselecteerd = niets
def test_unselected_share_indexes_nothing_and_never_leaves_nas(
    paired_client: Any, auth: Auth, shares: Shares, fake_hub: FakeHub
) -> None:
    client = paired_client
    before = shares.checksums()
    src = make_source(client, auth, str(shares.a))
    assert src["status"] == "draft"

    # Concept (nog geen toestemming): herindexeren weigert, zoeken levert niets.
    r = client.post("/search/v1/reindex", json={"source_id": src["id"]}, headers=idem(auth("admin", sub="beheer")))
    assert r.status_code == 409 and r.json()["code"] == "geen_toestemming"

    # Bronwizard: alleen share A; share B, symlinks naar B, .ssh, .zfs en sleutels nooit.
    pv = client.get(f"/search/v1/sources/{src['id']}/preview", headers=auth("admin", sub="beheer")).json()
    assert pv["inhoud_gelezen"] is False
    assert pv["bestanden"] == 2 and sorted(pv["voorbeeld"]) == ["documenten/notities.md", "documenten/werk/offerte.txt"]
    assert not any("prive-b" in s or "dagboek" in s or "id_ed25519" in s or ".zfs" in s for s in pv["voorbeeld"])
    assert pv["overgeslagen"]["symlink"] == 2

    # Met toestemming voor A: indexeren zelf is nog niet gebouwd en zegt dat eerlijk (geen gesimuleerd succes).
    assert consent(client, auth, src["id"]).status_code == 200
    r = client.post("/search/v1/reindex", json={"source_id": src["id"]}, headers=idem(auth("admin", sub="beheer")))
    assert r.status_code == 501 and r.json()["code"] == "capability_unavailable"  # ingest bestaat nog niet: eerlijk

    # Externe AI-verwerking kan niet eens worden aangevraagd: data verlaat de NAS niet.
    r = consent(client, auth, src["id"], scopes=["metadata.read", "ai.external"])
    assert r.status_code == 501 and r.json()["details"]["capability"] == "ai.external"

    # Zelfs als een (toekomstige) ingest per ongeluk B-data zou schrijven bij een bron zonder toestemming: onvindbaar.
    rogue = make_source(client, auth, str(shares.b), name="B (niet geselecteerd)", owner="user-a")
    with client.app.state.db.sessions() as s:
        simulate_ingest(s, rogue["id"], str(shares.b / "dagboek.txt"), ["Privé dagboek van B"])
    assert search(client, auth, "dagboek", "user-a")["items"] == []

    # Niets heeft de NAS verlaten: het enige uitgaande verkeer ging naar de Hub (koppelen/JWKS/events), zonder
    # bestandsnamen of inhoud.
    client.app.state.link.invalidate()
    from search_ai.worker import Worker

    w = Worker(client.app.state.settings, transport=fake_hub.transport)
    w.push_step()
    hosts = {r["host"] for r in fake_hub.all_requests}
    assert hosts == {"hub.test"}
    wire = json.dumps(fake_hub.all_requests)
    for needle in ("dagboek", "offerte", "Boodschappen", "prive-b", str(shares.root)):
        assert needle not in wire
    assert {r["path"] for r in fake_hub.all_requests} <= {
        "/api/v1/services/enroll",
        "/api/v1/auth/jwks",
        "/api/v1/events/inbox",
    }
    assert shares.checksums() == before  # originelen ongewijzigd


# ------------------------------------------------------------------ consent-register
def test_consent_only_by_owner_bound_to_snapshot(paired_client: Any, auth: Auth, shares: Shares) -> None:
    client = paired_client
    src = make_source(client, auth, str(shares.a))
    sid = src["id"]
    # beheerder kan niet namens de eigenaar instemmen; kinderprofiel nooit; onbekende gebruiker ziet niets
    r = consent(client, auth, sid, sub="beheer", roles=("admin",))
    assert r.status_code == 403 and r.json()["details"]["reason"] == "alleen_eigenaar"
    r = consent(client, auth, sid, sub="user-a", roles=("kind",))
    assert r.status_code == 403
    assert consent(client, auth, sid, sub="iemand-anders").status_code == 404
    # verouderde toestemmingstekst
    r = client.post(
        f"/search/v1/sources/{sid}/consent",
        json={"scopes": ["metadata.read"], "consent_text_version": "oud", "bevestiging": True},
        headers=idem(auth("viewer", sub="user-a")),
    )
    assert r.status_code == 409 and r.json()["code"] == "toestemmingstekst_verouderd"
    # scope die niet bij de bronsoort past / onbekend
    r = consent(client, auth, sid, scopes=["transcripts.read"])
    assert r.status_code == 422 and r.json()["code"] == "scope_niet_voor_bronsoort"
    # eigenaar geeft toestemming → actief, scopes vastgelegd, momentopname-hash
    r = consent(client, auth, sid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "active" and body["scopes"] == ["content.read", "metadata.read"]
    assert body["consent"]["snapshot_sha256"] and body["consent_matches_current"] is True

    admin = auth("admin", sub="beheer")
    etag = client.get(f"/search/v1/sources/{sid}", headers=admin).headers["ETag"]
    # paden wijzigen terwijl actief → eerst pauzeren
    r = client.patch(
        f"/search/v1/sources/{sid}", json={"allowed_paths": [str(shares.a / "werk")]}, headers=idem(admin, **{"If-Match": etag})
    )
    assert r.status_code == 409 and r.json()["code"] == "bron_actief_eerst_pauzeren"
    # zonder If-Match → 428
    r = client.patch(f"/search/v1/sources/{sid}", json={"name": "x"}, headers=idem(admin))
    assert r.status_code == 428
    assert client.post(f"/search/v1/sources/{sid}/pause", headers=idem(admin)).status_code == 200
    etag = client.get(f"/search/v1/sources/{sid}", headers=admin).headers["ETag"]
    r = client.patch(
        f"/search/v1/sources/{sid}",
        json={"allowed_paths": [str(shares.a), str(shares.b)]},
        headers=idem(admin, **{"If-Match": etag}),
    )
    assert r.status_code == 200, r.text
    # verruimde selectie → oude toestemming past niet meer → hervatten geweigerd
    r = client.post(f"/search/v1/sources/{sid}/resume", headers=idem(admin))
    assert r.status_code == 409 and r.json()["code"] == "toestemming_verouderd"
    # opnieuw toestemming → oude wordt vervangen (één actieve per bron)
    assert consent(client, auth, sid).status_code == 200
    with client.app.state.db.sessions() as s:
        rows = list(s.scalars(select(PrivacyConsent).where(PrivacyConsent.source_id == uuid.UUID(sid))))
        assert len(rows) == 2 and sum(1 for c in rows if c.revoked_at is None) == 1
        perms = list(s.scalars(select(ConnectorPermission).where(ConnectorPermission.revoked_at.is_(None))))
        assert sorted(p.scope for p in perms) == ["content.read", "metadata.read"]


def test_source_validation_rejects_paths_outside_allowlist(paired_client: Any, auth: Auth, shares: Shares, tmp_path: Any) -> None:
    admin = idem(auth("admin", sub="beheer"))
    for path, code in [
        (str(tmp_path), "pad_buiten_leeswortels"),
        (str(shares.a / ".ssh"), "pad_uitgesloten"),
        ("relatief", "pad_niet_absoluut"),
        (str(shares.root) + "/../etc", "pad_met_punten"),
    ]:
        r = paired_client.post(
            "/search/v1/sources", json={"kind": "files", "name": "x", "allowed_paths": [path]}, headers=idem(admin)
        )
        assert r.status_code == 422 and r.json()["code"] == code, (path, r.text)
    # niet-beheerders kunnen geen bron selecteren
    r = paired_client.post(
        "/search/v1/sources",
        json={"kind": "files", "name": "x", "allowed_paths": [str(shares.a)]},
        headers=idem(auth("operator", sub="op")),
    )
    assert r.status_code == 403


def test_readonly_mount_required_for_consent(app_factory: Callable[..., Any], auth: Auth, shares: Shares) -> None:
    from tests.conftest import HUB_URL, PAIR_CODE

    client = app_factory(require_readonly_mounts=True)
    with client.app.state.db.sessions() as s:
        assert client.app.state.enrollment.enroll_with_code(s, HUB_URL, PAIR_CODE).ok
        s.commit()
    client.app.state.link.invalidate()
    src = make_source(client, auth, str(shares.a))
    r = consent(client, auth, src["id"])
    assert r.status_code == 409 and r.json()["code"] == "bron_niet_klaar"
    assert r.json()["details"]["problemen"][0]["probleem"] == "niet_alleen_lezen"
    # met een (gesimuleerde) alleen-lezen koppeling lukt het wel
    real_statvfs = os.statvfs

    class RO:
        def __init__(self, path: str) -> None:
            self.f_flag = real_statvfs(path).f_flag | os.ST_RDONLY

    import search_ai.search.paths as paths_mod

    orig = paths_mod.os.statvfs
    paths_mod.os.statvfs = RO  # type: ignore[assignment]
    try:
        assert consent(client, auth, src["id"]).status_code == 200
    finally:
        paths_mod.os.statvfs = orig  # type: ignore[assignment]


# ------------------------------------------------------------------ ACL vóór ranking
def test_acl_applied_before_ranking(paired_client: Any, auth: Auth, shares: Shares) -> None:
    client = paired_client
    a = make_source(client, auth, str(shares.a), owner="user-a", name="A")
    b = make_source(client, auth, str(shares.b), owner="user-b", name="B")
    assert consent(client, auth, a["id"], sub="user-a").status_code == 200
    assert consent(client, auth, b["id"], sub="user-b").status_code == 200
    with client.app.state.db.sessions() as s:
        simulate_ingest(s, a["id"], str(shares.a / "notities.md"), ["keuken plannen"])
        # B's document scoort veel hoger op 'keuken' (vaker genoemd) — mag A's ranking/limiet niet beïnvloeden
        simulate_ingest(s, b["id"], str(shares.b / "dagboek.txt"), ["keuken keuken keuken keuken geheim van B"])

    res_a = search(client, auth, "keuken", "user-a")
    assert [i["bron"]["id"] for i in res_a["items"]] == [a["id"]]
    assert res_a["doorzochte_bronnen"] == 1
    res_b = search(client, auth, "keuken", "user-b")
    assert [i["bron"]["id"] for i in res_b["items"]] == [b["id"]]
    assert "geheim" not in json.dumps(res_a)
    # beheerder zonder ACL-regel ziet geen inhoud; iemand zonder rechten ook niet
    assert search(client, auth, "keuken", "beheer", "admin")["items"] == []
    assert search(client, auth, "keuken", "user-c")["items"] == []
    # leesrecht per rol: alle viewers zien A (maar nog steeds niets van B)
    r = client.post(
        f"/search/v1/sources/{a['id']}/access",
        json={"principal_type": "role", "principal_id": "viewer"},
        headers=idem(auth("admin", sub="beheer")),
    )
    assert r.status_code == 201
    access_id = r.json()["id"]
    assert [i["bron"]["id"] for i in search(client, auth, "keuken", "user-c")["items"]] == [a["id"]]
    # intrekken van die regel → weer niets
    r = client.delete(f"/search/v1/sources/{a['id']}/access/{access_id}", headers=idem(auth("admin", sub="beheer")))
    assert r.status_code == 200
    assert search(client, auth, "keuken", "user-c")["items"] == []
    # semantisch zoeken zonder lokaal model: eerlijk 501
    r = client.get("/search/v1/search", params={"q": "keuken", "mode": "semantic"}, headers=auth("viewer", sub="user-a"))
    assert r.status_code == 501
    # een service-token heeft geen gebruikerscontext → mag niet zoeken
    token = fake_token(client)
    r = client.get("/search/v1/search", params={"q": "keuken"}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403 and r.json()["details"]["reason"] == "gebruikerscontext_vereist"


def fake_token(client: Any) -> str:
    with client.app.state.db.sessions() as s:
        tok = client.app.state.enrollment.service_token(s)
    assert tok
    return str(tok)


def test_owner_only_documents_and_paused_sources(paired_client: Any, auth: Auth, shares: Shares) -> None:
    client = paired_client
    src = make_source(client, auth, str(shares.a), owner="user-a")
    assert consent(client, auth, src["id"]).status_code == 200
    client.post(
        f"/search/v1/sources/{src['id']}/access",
        json={"principal_type": "role", "principal_id": "viewer"},
        headers=idem(auth("admin", sub="beheer")),
    )
    with client.app.state.db.sessions() as s:
        simulate_ingest(s, src["id"], str(shares.a / "werk" / "offerte.txt"), ["offerte gedeeld"])
        simulate_ingest(
            s, src["id"], str(shares.a / "notities.md"), ["offerte privé"], visibility="owner", owner_sub="user-a"
        )
    assert len(search(client, auth, "offerte", "user-a")["items"]) == 2
    assert [i["fragment"] for i in search(client, auth, "offerte", "user-c")["items"]] == ["«offerte» gedeeld"]
    # gepauzeerd = niet doorzoekbaar
    assert client.post(f"/search/v1/sources/{src['id']}/pause", headers=idem(auth("admin", sub="beheer"))).status_code == 200
    assert search(client, auth, "offerte", "user-a")["items"] == []
    assert client.post(f"/search/v1/sources/{src['id']}/resume", headers=idem(auth("admin", sub="beheer"))).status_code == 200
    assert len(search(client, auth, "offerte", "user-a")["items"]) == 2


# ------------------------------------------------------------------ kinderprofiel
def test_kid_profile_restrictions(paired_client: Any, auth: Auth, shares: Shares) -> None:
    client = paired_client
    admin = auth("admin", sub="beheer")
    sensitive = make_source(client, auth, str(shares.b), owner="ouder", name="Gevoelig")
    # rol 'kind' toevoegen aan een bron die niet voor kinderen is opengezet → geweigerd
    r = client.post(
        f"/search/v1/sources/{sensitive['id']}/access",
        json={"principal_type": "role", "principal_id": "kind"},
        headers=idem(admin),
    )
    assert r.status_code == 422 and r.json()["code"] == "kinderprofiel_niet_toegestaan"
    # kinderprofiel + gevoelige classificatie gaat nooit samen
    r = client.post(
        "/search/v1/sources",
        json={"kind": "files", "name": "x", "allowed_paths": [str(shares.a)], "kid_allowed": True},
        headers=idem(admin),
    )
    assert r.status_code == 422 and r.json()["code"] == "kinderprofiel_niet_bij_gevoelig"

    family = make_source(
        client, auth, str(shares.a), owner="ouder", name="Gezin", kid_allowed=True, classification="internal"
    )
    assert consent(client, auth, family["id"], sub="ouder").status_code == 200
    assert consent(client, auth, sensitive["id"], sub="ouder").status_code == 200
    r = client.post(
        f"/search/v1/sources/{family['id']}/access",
        json={"principal_type": "role", "principal_id": "kind"},
        headers=idem(admin),
    )
    assert r.status_code == 201
    # ook een expliciete gebruikersregel voor het kind op de gevoelige bron helpt niet: kind-beperkingen winnen
    client.post(
        f"/search/v1/sources/{sensitive['id']}/access",
        json={"principal_type": "user", "principal_id": "kind-1"},
        headers=idem(admin),
    )
    with client.app.state.db.sessions() as s:
        simulate_ingest(s, family["id"], str(shares.a / "notities.md"), ["dierentuin uitje"], kid_safe=True, classification="internal")
        simulate_ingest(s, family["id"], str(shares.a / "werk" / "offerte.txt"), ["dierentuin kosten"], kid_safe=False)
        simulate_ingest(s, sensitive["id"], str(shares.b / "dagboek.txt"), ["dierentuin dagboek"], kid_safe=True)
    kid = search(client, auth, "dierentuin", "kind-1", "kind")
    assert [i["fragment"] for i in kid["items"]] == ["«dierentuin» uitje"]
    # 'kind' wint ook als het token daarnaast 'admin' heeft
    both = search(client, auth, "dierentuin", "kind-1", "kind", "admin")
    assert [i["fragment"] for i in both["items"]] == ["«dierentuin» uitje"]
    kid_hdr = auth("kind", "admin", sub="kind-1")
    assert client.post("/search/v1/sources", json={"kind": "logs", "name": "x", "allowed_paths": [str(shares.a)]}, headers=idem(kid_hdr)).status_code == 403
    assert client.get("/search/v1/privacy/coverage", headers=kid_hdr).status_code == 403
    assert client.post(f"/search/v1/sources/{family['id']}/revoke", headers=idem(auth("kind", sub="ouder"))).status_code == 404


# ------------------------------------------------------------------ intrekken = opschonen
def test_revoke_purges_index_and_embeddings_but_not_originals(
    paired_client: Any, auth: Auth, shares: Shares, fake_hub: FakeHub
) -> None:
    client = paired_client
    src = make_source(client, auth, str(shares.a))
    sid = src["id"]
    assert consent(client, auth, sid).status_code == 200
    with client.app.state.db.sessions() as s:
        simulate_ingest(s, sid, str(shares.a / "notities.md"), ["appels en peren", "nog een fragment"])
        simulate_ingest(s, sid, str(shares.a / "werk" / "offerte.txt"), ["offerte keuken"], timecodes=[(1000, 2000)])
        before_counts = counts(s, sid)
    assert before_counts == {"documenten": 2, "chunks": 3, "embeddings": 3, "transcripten": 1}
    assert len(search(client, auth, "appels", "user-a")["items"]) == 1
    before = shares.checksums()

    key = str(uuid.uuid4())
    r = client.post(f"/search/v1/sources/{sid}/revoke", json={"reden": "test"}, headers=idem(auth("viewer", sub="user-a"), key=key))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bron"]["status"] == "revoked"
    assert body["opschonen"]["status"] == "succeeded"
    assert body["opschonen"]["counts"]["verwijderd"] == before_counts
    assert body["opschonen"]["verification"]["leeg"] is True
    assert body["opschonen"]["verification"]["na_verwijderen"] == {"documenten": 0, "chunks": 0, "embeddings": 0, "transcripten": 0}
    # herhaling met dezelfde key → zelfde antwoord, geen tweede uitvoering
    r2 = client.post(f"/search/v1/sources/{sid}/revoke", json={"reden": "test"}, headers=idem(auth("viewer", sub="user-a"), key=key))
    assert r2.json() == body and r2.headers.get("Idempotent-Replay") == "true"

    with client.app.state.db.sessions() as s:
        assert counts(s, sid) == {"documenten": 0, "chunks": 0, "embeddings": 0, "transcripten": 0}
        src_row = s.get(IndexSource, uuid.UUID(sid))
        assert src_row is not None and src_row.purge_status == "done" and src_row.purge_verified_at is not None
        assert s.scalars(select(PrivacyConsent).where(PrivacyConsent.revoked_at.is_(None))).first() is None
        assert s.scalars(select(ConnectorPermission).where(ConnectorPermission.revoked_at.is_(None))).first() is None
        assert s.scalars(select(AccessScope).where(AccessScope.revoked_at.is_(None))).first() is None
        runs = list(s.scalars(select(IndexRun).where(IndexRun.kind == "purge")))
        assert len(runs) == 1
    assert search(client, auth, "appels", "user-a")["items"] == []
    assert shares.checksums() == before  # originelen niet aangeraakt

    # na intrekken: geen nieuwe toestemming, geen ACL, geen wijziging
    assert consent(client, auth, sid).status_code == 409
    # bewijs via de connector (geredigeerd) en event zonder inhoud
    run_id = body["opschonen"]["id"]
    tok = fake_token(client)
    ev = client.get(f"/connector/v1/evidence/{run_id}", headers={"Authorization": f"Bearer {tok}"})
    assert ev.status_code == 200 and ev.json()["kind"] == "index_run"
    events = client.get("/connector/v1/events", headers={"Authorization": f"Bearer {tok}"}).json()["items"]
    revoked = [e for e in events if e["event_type"] == "search.source.revoked"]
    assert len(revoked) == 1 and revoked[0]["payload"]["purge_verified"] is True
    assert "appels" not in json.dumps(events) and str(shares.root) not in json.dumps(events)

    # nacontrole: als er toch weer afgeleide data opduikt (bv. een racende ingest), ruimt de worker die op
    with client.app.state.db.sessions() as s:
        simulate_ingest(s, sid, str(shares.a / "notities.md"), ["achtergebleven"])
        s.get(IndexSource, uuid.UUID(sid)).purge_status = "failed"  # type: ignore[union-attr]
        s.commit()
        assert sweep_revoked(s) == 1
        s.commit()
        assert counts(s, sid)["documenten"] == 0


def test_narrowing_selection_purges_outside_docs(paired_client: Any, auth: Auth, shares: Shares) -> None:
    client = paired_client
    admin = auth("admin", sub="beheer")
    src = make_source(client, auth, str(shares.a))
    sid = src["id"]
    assert consent(client, auth, sid).status_code == 200
    with client.app.state.db.sessions() as s:
        simulate_ingest(s, sid, str(shares.a / "notities.md"), ["boven"])
        simulate_ingest(s, sid, str(shares.a / "werk" / "offerte.txt"), ["werk"])
    client.post(f"/search/v1/sources/{sid}/pause", headers=idem(admin))
    etag = client.get(f"/search/v1/sources/{sid}", headers=admin).headers["ETag"]
    r = client.patch(
        f"/search/v1/sources/{sid}", json={"allowed_paths": [str(shares.a / "werk")]}, headers=idem(admin, **{"If-Match": etag})
    )
    assert r.status_code == 200, r.text
    assert r.json()["purge"]["counts"]["verwijderd"]["documenten"] == 1
    with client.app.state.db.sessions() as s:
        assert counts(s, sid)["documenten"] == 1


def test_delete_index_keeps_consent(paired_client: Any, auth: Auth, shares: Shares) -> None:
    client = paired_client
    admin = auth("admin", sub="beheer")
    src = make_source(client, auth, str(shares.a))
    sid = src["id"]
    assert consent(client, auth, sid).status_code == 200
    with client.app.state.db.sessions() as s:
        simulate_ingest(s, sid, str(shares.a / "notities.md"), ["x"])
    r = client.delete(f"/search/v1/sources/{sid}/index", headers=idem(admin))
    assert r.status_code == 200 and r.json()["status"] == "succeeded"
    detail = client.get(f"/search/v1/sources/{sid}", headers=admin).json()
    assert detail["status"] == "active" and detail["derived"]["documenten"] == 0 and detail["consent"] is not None


def test_privacy_coverage_and_visibility_of_sources(paired_client: Any, auth: Auth, shares: Shares) -> None:
    client = paired_client
    a = make_source(client, auth, str(shares.a), owner="user-a")
    make_source(client, auth, str(shares.b), owner="user-b", name="B")
    cov = client.get("/search/v1/privacy/coverage", headers=auth("viewer", sub="user-a")).json()
    assert [i["id"] for i in cov["items"]] == [a["id"]]
    assert cov["externe_ai"] == "capability_unavailable" and all(i["verlaat_nas"] is False for i in cov["items"])
    assert cov["zoekgeschiedenis_bewaren_dagen"] == 0
    full = client.get("/search/v1/privacy/coverage", headers=auth("admin", sub="beheer")).json()
    assert len(full["items"]) == 2
    # andere gebruiker ziet bron van A niet (404, geen bestaan lekken) en de lijst toont alleen eigen bronnen
    assert client.get(f"/search/v1/sources/{a['id']}", headers=auth("viewer", sub="user-b")).status_code == 404
    lst = client.get("/search/v1/sources", headers=auth("viewer", sub="user-b")).json()["items"]
    assert [i["name"] for i in lst] == ["B"]
    assert "allowed_paths" in lst[0]  # eigenaar ziet zijn eigen paden
    # ACL-houder (geen eigenaar) ziet geen paden
    consent(client, auth, a["id"], sub="user-a")
    client.post(
        f"/search/v1/sources/{a['id']}/access",
        json={"principal_type": "user", "principal_id": "user-b"},
        headers=idem(auth("admin", sub="beheer")),
    )
    view = client.get(f"/search/v1/sources/{a['id']}", headers=auth("viewer", sub="user-b")).json()
    assert "allowed_paths" not in view and "owner_sub" not in view


def test_search_history_not_stored_by_default(paired_client: Any, auth: Auth, shares: Shares) -> None:
    from sqlalchemy import func

    from search_ai.search.models import SearchQuery

    res = search(paired_client, auth, "iets", "user-a")
    assert res["zoekgeschiedenis_bewaard"] is False
    with paired_client.app.state.db.sessions() as s:
        assert s.scalar(select(func.count()).select_from(SearchQuery)) == 0
