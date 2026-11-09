"""Onafhankelijke review P0 (QA): regressietests voor de extra aandachtspunten.

- ACL vóór ranking, ook met ``limit``/paginering (rijen van anderen mogen ranking, aantallen en ``next_cursor`` niet
  beïnvloeden);
- ACL op de **vectorindex** (B07-003-acceptatie: *gebruiker B kan niet in een privébestand van A zoeken via de
  vectorindex*), ook met een echte HNSW-index van pgvector;
- kinderprofiel ziet nergens iets geblokkeerds (zoeken, bronnenlijst, details, voorvertoning, runs, privacy,
  connector, vectorindex);
- intrekken laat **geen restdata** achter: de hele database wordt doorzocht op de geïndexeerde inhoud en
  bestandsnaam; ook bewaarde zoekgeschiedenis en B15-context van de bron verdwijnen; originelen ongewijzigd;
- samengestelde bron-sleutels (migratie 0002): afgeleide rijen kunnen niet aan een andere bron hangen;
- verzoek- en tijdlimiet voor zoeken (B07 §3).

Indexrijen worden geschreven met ``simulate_ingest`` (simulatie van de nog niet gebouwde ingest B07-002).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from search_ai.errors import ApiError
from search_ai.search import fulltext, semantic
from search_ai.search.models import Chunk, Embedding, IndexSource, Retention, SearchQuery, TranscriptRef
from search_ai.search.service import sweep_revoked
from search_ai.security.auth import Principal
from search_ai.timeutil import utcnow
from search_ai.voice.models import ContextScope
from tests.conftest import FakeHub, Shares, idem, pair
from tests.integration.helpers import counts, simulate_ingest
from tests.integration.test_b07_001_consent import consent, make_source

pytestmark = pytest.mark.integration

Auth = Callable[..., dict[str, str]]
MODEL = ("test-model", "0")
QUERY = [1.0, 0.0, 0.0]


def user(sub: str, *roles: str) -> Principal:
    return Principal(kind="user", subject=sub, roles=frozenset(roles or ("viewer",)))


def grant(client: Any, auth: Auth, sid: str, ptype: str, pid: str) -> str:
    r = client.post(
        f"/search/v1/sources/{sid}/access",
        json={"principal_type": ptype, "principal_id": pid},
        headers=idem(auth("admin", sub="beheer")),
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def get_search(client: Any, auth: Auth, sub: str, *roles: str, **params: Any) -> dict[str, Any]:
    r = client.get("/search/v1/search", params=params, headers=auth(*(roles or ("viewer",)), sub=sub))
    assert r.status_code == 200, r.text
    return dict(r.json())


def nearest(client: Any, principal: Principal, query: list[float] | None = None, limit: int = 10) -> list[Any]:
    with client.app.state.db.sessions() as s:
        return semantic.nearest_chunks(
            s, principal, query or QUERY, model_name=MODEL[0], model_version=MODEL[1], limit=limit
        )


# ------------------------------------------------------------------ ACL vóór ranking, ook met limiet/paginering
def test_acl_before_ranking_survives_limit_and_pagination(paired_client: Any, auth: Auth, shares: Shares) -> None:
    client = paired_client
    a = make_source(client, auth, str(shares.a), owner="user-a", name="A")
    b = make_source(client, auth, str(shares.b), owner="user-b", name="B")
    assert consent(client, auth, a["id"], sub="user-a").status_code == 200
    assert consent(client, auth, b["id"], sub="user-b").status_code == 200
    with client.app.state.db.sessions() as s:
        simulate_ingest(s, a["id"], str(shares.a / "notities.md"), ["keuken"], with_embedding=False)
        for i in range(30):  # 30 documenten van B die allemaal veel hoger scoren op 'keuken'
            simulate_ingest(
                s, b["id"], str(shares.b / f"dagboek-{i}.txt"), ["keuken keuken keuken geheim"], with_embedding=False
            )

    # A met limit=1: krijgt zijn eigen (laag scorende) treffer; geen 'er is meer' door rijen van B
    body = get_search(client, auth, "user-a", q="keuken", limit=1)
    assert [i["bron"]["id"] for i in body["items"]] == [a["id"]]
    assert body["next_cursor"] is None and body["doorzochte_bronnen"] == 1
    assert "geheim" not in json.dumps(body)

    # B bladert door precies zijn eigen 30 treffers, nooit die van A
    seen: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(10):
        params: dict[str, Any] = {"q": "keuken", "limit": 7}
        if cursor:
            params["cursor"] = cursor
        page = get_search(client, auth, "user-b", **params)
        seen += page["items"]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == 30 and {i["bron"]["id"] for i in seen} == {b["id"]}
    assert len({i["chunk_id"] for i in seen}) == 30  # geen dubbelen over de pagina's heen

    # beheerder zonder ACL-regel: niets, ook geen 'next_cursor'
    body = get_search(client, auth, "beheer", "admin", q="keuken", limit=1)
    assert body["items"] == [] and body["next_cursor"] is None and body["doorzochte_bronnen"] == 0
    # filteren op de bron van een ander geeft niets (en verraadt het bestaan niet)
    assert get_search(client, auth, "user-a", q="keuken", source_id=b["id"])["items"] == []


# ------------------------------------------------------------------ B07-003: ACL op de vectorindex
def test_vector_index_acl_b07_003(paired_client: Any, auth: Auth, shares: Shares, clean_db: str) -> None:
    """Acceptatie B07-003: *Gebruiker B kan niet in een privébestand van A zoeken via de vectorindex.*"""
    client = paired_client
    a = make_source(client, auth, str(shares.a), owner="user-a", name="A")
    b = make_source(client, auth, str(shares.b), owner="user-b", name="B")
    gezin = make_source(
        client, auth, str(shares.a / "werk"), owner="ouder", name="Gezin", kid_allowed=True, classification="internal"
    )
    for sid, sub in ((a["id"], "user-a"), (b["id"], "user-b"), (gezin["id"], "ouder")):
        assert consent(client, auth, sid, sub=sub).status_code == 200
    grant(client, auth, a["id"], "user", "user-b")  # B mag de gedeelde bron A lezen, niet A's privébestanden
    grant(client, auth, gezin["id"], "role", "kind")
    with client.app.state.db.sessions() as s:
        # A's privébestand is exact gelijk aan de querievector (afstand 0)
        private = simulate_ingest(
            s,
            a["id"],
            str(shares.a / "notities.md"),
            ["privé van A"],
            visibility="owner",
            owner_sub="user-a",
            vectors=[[1.0, 0.0, 0.0]],
        )
        shared = simulate_ingest(
            s, a["id"], str(shares.a / "werk" / "offerte.txt"), ["gedeeld"], vectors=[[0.6, 0.8, 0]]
        )
        own_b = simulate_ingest(s, b["id"], str(shares.b / "dagboek.txt"), ["van B"], vectors=[[0.0, 1.0, 0.0]])
        # zelfde vector maar ander model: telt nooit mee
        simulate_ingest(
            s,
            a["id"],
            str(shares.a / "ander-model.txt"),
            ["ander model"],
            visibility="owner",
            owner_sub="user-a",
            vectors=[[1.0, 0.0, 0.0]],
            model=("test-model", "1"),
        )
        kid_ok = simulate_ingest(
            s,
            gezin["id"],
            str(shares.a / "werk" / "dierentuin.txt"),
            ["dierentuin"],
            kid_safe=True,
            classification="internal",
            vectors=[[0.9, 0.1, 0.0]],
        )
        kid_blocked = simulate_ingest(
            s, gezin["id"], str(shares.a / "werk" / "volwassen.txt"), ["volwassen"], vectors=[[1.0, 0.01, 0.0]]
        )

    def docs(hits: list[Any]) -> list[uuid.UUID]:
        return [h.document_id for h in hits]

    # A vindt zijn eigen privébestand als dichtstbijzijnde
    assert docs(nearest(client, user("user-a")))[0] == private.id
    # B: nooit A's privébestand — ook niet met limit=1 terwijl dat de exacte match is
    hb = nearest(client, user("user-b"))
    assert private.id not in docs(hb)
    assert set(docs(hb)) == {shared.id, own_b.id}
    assert docs(nearest(client, user("user-b"), limit=1)) == [shared.id]
    # beheerder zonder ACL, onbekende gebruiker en service-token: niets
    assert nearest(client, user("beheer", "admin")) == []
    assert nearest(client, user("user-c")) == []
    assert nearest(client, Principal(kind="service", subject="hub", roles=frozenset({"service"}))) == []
    # andere dimensie: niets (geen fout, geen andere modellen)
    assert nearest(client, user("user-a"), query=[1.0, 0.0]) == []
    # kinderprofiel: alleen kid_safe in een voor kinderen opengezette, niet-gevoelige bron (ook met rol admin)
    assert docs(nearest(client, user("kind-1", "kind"))) == [kid_ok.id]
    assert docs(nearest(client, user("kind-1", "kind", "admin"))) == [kid_ok.id]
    assert kid_blocked.id not in docs(nearest(client, user("kind-1", "kind")))

    # Met een echte HNSW-index (pgvector) op dezelfde expressie: zelfde ACL-uitkomst, en de index wordt gebruikt.
    eng = create_engine(clean_db)
    with eng.begin() as c:
        c.execute(
            text(
                "CREATE INDEX test_hnsw_cos ON embedding USING hnsw ((vector::vector(3)) vector_cosine_ops) "
                "WHERE model_name = 'test-model' AND model_version = '0' AND dimension = 3"
            )
        )
    try:
        with client.app.state.db.sessions() as s:
            # kleine testtabel: zonder deze hints kiest de planner terecht een gewone scan + sorteren
            s.execute(text("SET LOCAL enable_seqscan = off"))
            s.execute(text("SET LOCAL enable_sort = off"))
            stmt = semantic.nearest_statement(user("user-b"), QUERY, model_name=MODEL[0], model_version=MODEL[1])
            compiled = stmt.compile(dialect=s.get_bind().dialect, compile_kwargs={"render_postcompile": True})
            plan = "\n".join(r[0] for r in s.connection().exec_driver_sql("EXPLAIN " + str(compiled), compiled.params))
            assert "test_hnsw_cos" in plan, plan
            hits = semantic.nearest_chunks(s, user("user-b"), QUERY, model_name=MODEL[0], model_version=MODEL[1])
            assert private.id not in docs(hits) and set(docs(hits)) <= {shared.id, own_b.id}
    finally:
        with eng.begin() as c:
            c.execute(text("DROP INDEX IF EXISTS test_hnsw_cos"))
        eng.dispose()

    # na intrekken van A: A's vectoren zijn weg, ook voor de eigenaar
    r = client.post(f"/search/v1/sources/{a['id']}/revoke", headers=idem(auth("viewer", sub="user-a")))
    assert r.status_code == 200 and r.json()["opschonen"]["status"] == "succeeded"
    assert nearest(client, user("user-a")) == []
    assert docs(nearest(client, user("user-b"))) == [own_b.id]


# ------------------------------------------------------------------ kinderprofiel ziet nergens iets geblokkeerds
def test_kid_profile_sees_nothing_blocked_anywhere(paired_client: Any, auth: Auth, shares: Shares) -> None:
    client = paired_client
    gevoelig = make_source(client, auth, str(shares.b), owner="ouder", name="Gevoelig dagboek")
    eigen = make_source(client, auth, str(shares.b), owner="kind-1", name="Eigen bron van het kind")
    gezin = make_source(
        client, auth, str(shares.a), owner="ouder", name="Gezin", kid_allowed=True, classification="internal"
    )
    assert consent(client, auth, gevoelig["id"], sub="ouder").status_code == 200
    assert consent(client, auth, gezin["id"], sub="ouder").status_code == 200
    # het kind kan zelf geen toestemming geven, ook niet voor 'eigen' gegevens
    assert consent(client, auth, eigen["id"], sub="kind-1", roles=("kind",)).status_code == 403
    grant(client, auth, gezin["id"], "role", "kind")
    grant(client, auth, gevoelig["id"], "user", "kind-1")  # expliciete regel helpt niet: kind-beperkingen winnen
    with client.app.state.db.sessions() as s:
        simulate_ingest(
            s, gezin["id"], str(shares.a / "notities.md"), ["zoo uitje"], kid_safe=True, classification="internal"
        )
        simulate_ingest(s, gezin["id"], str(shares.a / "werk" / "offerte.txt"), ["zoo volwassen"], kid_safe=False)
        simulate_ingest(  # kid_safe maar zelf als gevoelig geclassificeerd → toch geblokkeerd
            s, gezin["id"], str(shares.a / "gevoelig.txt"), ["zoo gevoelig"], kid_safe=True, classification="sensitive"
        )
        simulate_ingest(s, gevoelig["id"], str(shares.b / "dagboek.txt"), ["zoo dagboek"], kid_safe=True)

    for roles in (("kind",), ("kind", "admin"), ("kind", "viewer", "operator")):
        hdr = auth(*roles, sub="kind-1")
        res = client.get("/search/v1/search", params={"q": "zoo"}, headers=hdr).json()
        assert [i["fragment"] for i in res["items"]] == ["«zoo» uitje"], roles
        res = client.get("/search/v1/search", params={"q": "zoo", "source_id": gevoelig["id"]}, headers=hdr).json()
        assert res["items"] == []
        names = [i["name"] for i in client.get("/search/v1/sources", headers=hdr).json()["items"]]
        assert names == ["Gezin"], roles
        detail = client.get(f"/search/v1/sources/{gezin['id']}", headers=hdr).json()
        assert "allowed_paths" not in detail and "access" not in detail and "owner_sub" not in detail
        for sid in (gevoelig["id"], eigen["id"]):
            for suffix in ("", "/preview", "/runs"):
                assert client.get(f"/search/v1/sources/{sid}{suffix}", headers=hdr).status_code == 404, (sid, suffix)
        assert client.get("/search/v1/privacy/coverage", headers=hdr).status_code == 403
        assert client.get("/search/v1/retention", headers=hdr).status_code == 403
        for path in ("/connector/v1/discover", "/connector/v1/health", "/connector/v1/events"):
            assert client.get(path, headers=hdr).status_code == 403, path
        r = client.get("/connector/v1/items", params={"type": "search.source"}, headers=hdr)
        assert r.status_code == 403
        # geen beheerhandelingen, ook niet met rol admin erbij
        r = client.post(
            f"/search/v1/sources/{gezin['id']}/access",
            json={"principal_type": "user", "principal_id": "kind-1"},
            headers=idem(hdr),
        )
        assert r.status_code == 403
        assert client.post(f"/search/v1/sources/{gezin['id']}/pause", headers=idem(hdr)).status_code == 403
        assert client.delete(f"/search/v1/sources/{gezin['id']}/index", headers=idem(hdr)).status_code == 403


# ------------------------------------------------------------------ intrekken laat geen restdata achter
def _all_rows_as_text(clean_db: str) -> dict[str, str]:
    eng = create_engine(clean_db)
    out: dict[str, str] = {}
    with eng.connect() as c:
        tables = [
            r[0]
            for r in c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"))
        ]
        for t in tables:
            rows = c.execute(text(f'SELECT row_to_json(x)::text FROM "{t}" x')).scalars().all()  # noqa: S608
            out[t] = "\n".join(rows)
    eng.dispose()
    return out


def test_revoke_leaves_no_rest_data_anywhere(paired_client: Any, auth: Auth, shares: Shares, clean_db: str) -> None:
    client = paired_client
    secret = "VERTROUWELIJK-qx7-inhoud"
    with client.app.state.db.sessions() as s:  # zoekgeschiedenis bewaren (opt-in), zodat ook die opgeschoond moet
        s.execute(delete(Retention).where(Retention.scope == "search_query", Retention.source_id.is_(None)))
        s.add(Retention(scope="search_query", source_id=None, keep_days=30, updated_by="test"))
        s.commit()
    before = shares.checksums()
    a = make_source(client, auth, str(shares.a), owner="user-a", name="A")
    b = make_source(client, auth, str(shares.b), owner="user-b", name="B")  # controle: blijft ongemoeid
    assert consent(client, auth, a["id"], sub="user-a").status_code == 200
    assert consent(client, auth, b["id"], sub="user-b").status_code == 200
    with client.app.state.db.sessions() as s:
        simulate_ingest(
            s, a["id"], str(shares.a / "notities.md"), [f"{secret} keuken"], timecodes=[(0, 1000)], title="notities.md"
        )
        simulate_ingest(s, b["id"], str(shares.b / "dagboek.txt"), ["keuken van B"])
        s.add(  # B15-context op deze bron (schema klaar; logica volgt in P5)
            ContextScope(
                principal_sub="user-a",
                context_kind="search_source",
                context_ref="bron-a",
                search_source_id=uuid.UUID(a["id"]),
                granted_by="test",
                expires_at=utcnow() + timedelta(days=1),
            )
        )
        s.commit()
    assert get_search(client, auth, "user-a", q="keuken")["zoekgeschiedenis_bewaard"] is True
    assert get_search(client, auth, "user-b", q="keuken")["zoekgeschiedenis_bewaard"] is True
    assert secret in "\n".join(_all_rows_as_text(clean_db).values())  # de toets ziet de inhoud wél vóór intrekken

    r = client.post(f"/search/v1/sources/{a['id']}/revoke", headers=idem(auth("viewer", sub="user-a")))
    assert r.status_code == 200, r.text
    ver = r.json()["opschonen"]["verification"]
    assert ver["leeg"] is True and ver["zoekgeschiedenis_opgeschoond"] is True
    assert "zoekgeschiedenis" not in json.dumps(r.json()["opschonen"]["counts"])  # geen telling van andermans zoeken

    dump = _all_rows_as_text(clean_db)
    everything = "\n".join(dump.values())
    # inhoud, bestandsnaam en tijdcode-transcript van A staan nergens meer in de database (ook niet in audit, outbox,
    # idempotency-antwoorden of index_run)
    for needle in (secret, "notities.md", "notities.md.srt"):
        assert needle not in everything, [t for t, v in dump.items() if needle in v]
    with client.app.state.db.sessions() as s:
        assert counts(s, a["id"]) == {"documenten": 0, "chunks": 0, "embeddings": 0, "transcripten": 0}
        hist = list(s.scalars(select(SearchQuery)))
        assert [h.principal_sub for h in hist] == ["user-b"]  # alleen de geschiedenis van B (bron B) blijft
        ctx = s.scalars(select(ContextScope)).all()
        assert len(ctx) == 1 and ctx[0].revoked_at is not None  # spraakcontext op de bron ingetrokken
        assert counts(s, b["id"])["chunks"] == 1  # andere bron ongemoeid
        assert sweep_revoked(s) == 0  # nacontrole vindt niets meer
    assert shares.checksums() == before  # B07-002: originele bestanden (checksum + mtime) ongewijzigd


# ------------------------------------------------------------------ samengestelde bron-sleutels (migratie 0002)
def test_derived_rows_cannot_cross_sources(clean_db: str) -> None:
    eng = create_engine(clean_db)

    def source(s: Session, name: str) -> IndexSource:
        src = IndexSource(
            kind="files", name=name, owner_sub="u", created_by="t", updated_by="t", allowed_paths=["/bronnen/x"]
        )
        s.add(src)
        s.flush()
        return src

    with Session(eng) as s:
        a, b = source(s, "A"), source(s, "B")
        doc = simulate_ingest(s, a.id, "/bronnen/x/a.txt", ["tekst"], with_embedding=True)
        chunk = s.scalars(select(Chunk).where(Chunk.document_id == doc.id)).one()
        a_id, b_id, doc_id, chunk_id = a.id, b.id, doc.id, chunk.id

    attempts: list[Any] = [
        Chunk(document_id=doc_id, source_id=b_id, ordinal=9, text="x", text_sha256="0" * 64),
        Embedding(
            chunk_id=chunk_id, source_id=b_id, model_name="m", model_version="v", dimension=3, vector=[1.0, 0, 0]
        ),
        TranscriptRef(document_id=doc_id, source_id=b_id, kind="subtitle", origin="existing", external_ref="x.srt"),
    ]
    for row in attempts:
        with Session(eng) as s:
            s.add(row)
            with pytest.raises(IntegrityError):
                s.commit()
    with Session(eng) as s:  # dezelfde bron mag wel
        s.add(
            Embedding(
                chunk_id=chunk_id, source_id=a_id, model_name="m", model_version="v", dimension=3, vector=[1, 0, 0]
            )
        )
        s.commit()
    eng.dispose()


# ------------------------------------------------------------------ verzoek- en tijdlimiet (B07 §3)
def test_search_rate_limit_and_statement_timeout(
    app_factory: Callable[..., Any], auth: Auth, shares: Shares, fake_hub: FakeHub
) -> None:
    client = app_factory(search_rate_limit=3)
    pair(client, fake_hub)
    for _ in range(3):
        assert client.get("/search/v1/search", params={"q": "x"}, headers=auth("viewer", sub="u1")).status_code == 200
    r = client.get("/search/v1/search", params={"q": "x"}, headers=auth("viewer", sub="u1"))
    assert r.status_code == 429 and r.json()["code"] == "rate_limited" and int(r.headers["Retry-After"]) >= 1
    assert "trace_id" in r.json()
    # een andere gebruiker heeft er geen last van
    assert client.get("/search/v1/search", params={"q": "x"}, headers=auth("viewer", sub="u2")).status_code == 200

    # tijdlimiet geldt per transactie …
    src = make_source(client, auth, str(shares.a), owner="u1")
    assert consent(client, auth, src["id"], sub="u1").status_code == 200
    with client.app.state.db.sessions() as s:
        fulltext.search(s, user("u1"), "x", source_id=None, kind=None, limit=5, cursor=None, statement_timeout_ms=4321)
        assert s.execute(text("SHOW statement_timeout")).scalar() == "4321ms"
    # … en een te zware zoekvraag wordt afgebroken met 503 (nooit een half antwoord)
    with client.app.state.db.sessions() as s:
        doc = simulate_ingest(s, src["id"], str(shares.a / "groot.txt"), ["keuken 0"], with_embedding=False)
        s.execute(
            text(
                "INSERT INTO chunk (id, document_id, source_id, ordinal, text, text_sha256, created_at) "
                "SELECT gen_random_uuid(), :d, :s, g, 'keuken ' || repeat('woord ', 50) || g, md5(g::text), now() "
                "FROM generate_series(1, 20000) g"
            ),
            {"d": doc.id, "s": uuid.UUID(src["id"])},
        )
        s.commit()
    with client.app.state.db.sessions() as s, pytest.raises(ApiError) as exc:
        fulltext.search(
            s, user("u1"), "keuken", source_id=None, kind=None, limit=5, cursor=None, statement_timeout_ms=1
        )
    assert exc.value.status_code == 503 and exc.value.code == "zoeken_tijdslimiet"


def test_migration_0002_keeps_valid_rows_and_drops_cross_source_rows() -> None:
    """Vooruit-migratie met data: geldige indexrijen blijven, bron-verwisselde (alleen door een fout mogelijk) niet."""
    from search_ai.config import Settings
    from search_ai.migrations import downgrade, upgrade
    from tests.conftest import recreate_database

    url = recreate_database("zoek_test_mig0002")
    settings = Settings(database_url=url)
    upgrade(settings, "0001")
    eng = create_engine(url)
    with Session(eng) as s:
        a = IndexSource(kind="files", name="A", owner_sub="u", created_by="t", updated_by="t", allowed_paths=["/b/a"])
        b = IndexSource(kind="files", name="B", owner_sub="u", created_by="t", updated_by="t", allowed_paths=["/b/b"])
        s.add_all([a, b])
        s.flush()
        good = simulate_ingest(s, a.id, "/b/a/goed.txt", ["goed"], with_embedding=True)
        bad = simulate_ingest(s, a.id, "/b/a/fout.txt", ["fout"], with_embedding=True)
        # onder 0001 kon een fout een chunk (met embedding) aan bron B hangen terwijl het document bij A hoort
        s.execute(
            text("UPDATE embedding SET source_id = :b WHERE chunk_id IN (SELECT id FROM chunk WHERE document_id = :d)"),
            {"b": b.id, "d": bad.id},
        )
        s.execute(text("UPDATE chunk SET source_id = :b WHERE document_id = :d"), {"b": b.id, "d": bad.id})
        s.commit()
        good_id, bad_id = good.id, bad.id
    upgrade(settings)  # → 0002
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM chunk WHERE document_id = :d"), {"d": good_id}).scalar() == 1
        assert c.execute(text("SELECT count(*) FROM embedding")).scalar() == 1
        assert c.execute(text("SELECT count(*) FROM chunk WHERE document_id = :d"), {"d": bad_id}).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM document_ref")).scalar() == 2  # documentverwijzingen blijven
    downgrade(settings, "0001")  # en terug kan ook
    upgrade(settings)
    eng.dispose()
