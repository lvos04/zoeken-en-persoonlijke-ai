"""QA P0-afronding (onafhankelijke review, ADR-0010): regressietests voor de gevonden gaten.

- **Symlink in het bronpad** (B07-001-acceptatie *niet geselecteerde share indexeert niets*): een gekozen pad dat
  onder de leeswortel via een symlink loopt (``/bronnen/a/link-naar-b/sub``), opende stilletjes share B. Nu: kiezen
  geweigerd, en een map die na het kiezen door een symlink is vervangen, wordt niet gelezen en krijgt geen toestemming.
- **Invoervalidatie:** een vervaldatum zonder tijdzone gaf een interne fout (500); namen/eigenaar/gebruiker van alleen
  spaties werden als lege waarde opgeslagen; een cursor met ``true`` gaf een stille offset of een databasefout.
- **Onbekend is nooit groen:** connector-health was 'healthy' terwijl de Hub-sleutels nog nooit waren opgehaald.
- **Kinderprofiel** kreeg als 'eigenaar' van een bron de beheerweergave (paden, toegangslijst).
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from search_ai.search.service import CONSENT_TEXT_VERSION
from tests.conftest import HUB_URL, PAIR_CODE, FakeHub, Shares, idem
from tests.integration.test_b07_001_consent import consent, make_source

pytestmark = pytest.mark.integration

Auth = Callable[..., dict[str, str]]


def _cursor(value: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps({"k": value}).encode()).rstrip(b"=").decode()


# ------------------------------------------------------------------ symlink in het bronpad (B07-001)
def test_selected_path_via_symlink_never_opens_unselected_share(paired_client: Any, auth: Auth, shares: Shares) -> None:
    client = paired_client
    admin = auth("admin", sub="beheer")
    (shares.b / "sub").mkdir()
    (shares.b / "sub" / "dagboek-2.txt").write_text("Privé van B", encoding="utf-8")
    before = shares.checksums()

    # 1. kiezen: een pad onder share A dat via de symlink 'map-naar-b' in share B uitkomt → 422
    via = shares.a / "map-naar-b" / "sub"
    r = client.post(
        "/search/v1/sources",
        json={"kind": "files", "name": "Via symlink", "allowed_paths": [str(via)], "owner_sub": "user-a"},
        headers=idem(admin),
    )
    assert r.status_code == 422 and r.json()["code"] == "pad_via_symlink", r.text
    assert r.json()["details"]["pad"] == str(via)

    # 2. een echte map kiezen en die daarna door een symlink naar share B vervangen (bv. via SMB door een gebruiker)
    os.mkdir(shares.a / "later")
    src = make_source(client, auth, str(shares.a / "later"), name="Later")
    os.rmdir(shares.a / "later")
    os.symlink(shares.b, shares.a / "later")
    owner = auth("viewer", sub="user-a")
    pv = client.get(f"/search/v1/sources/{src['id']}/preview", headers=owner).json()
    assert pv["bestanden"] == 0 and pv["voorbeeld"] == [], pv
    assert pv["paden"][0]["via_symlink"] is True and pv["paden"][0]["bereikbaar"] is False
    r = consent(client, auth, src["id"], sub="user-a")
    assert r.status_code == 409 and r.json()["code"] == "bron_niet_klaar", r.text
    assert r.json()["details"]["problemen"] == [{"pad": str(shares.a / "later"), "probleem": "via_symlink"}]
    detail = client.get(f"/search/v1/sources/{src['id']}", headers=owner).json()
    assert detail["status"] == "draft" and detail["consent"] is None
    assert shares.checksums() == before  # niets gewijzigd


# ------------------------------------------------------------------ invoervalidatie
def test_expires_at_without_timezone_is_422_not_500(paired_client: Any, auth: Auth, shares: Shares) -> None:
    src = make_source(paired_client, auth, str(shares.a))
    body = {
        "scopes": ["metadata.read"],
        "consent_text_version": CONSENT_TEXT_VERSION,
        "bevestiging": True,
        "expires_at": "2099-01-01T00:00:00",  # zonder tijdzone
    }
    r = paired_client.post(
        f"/search/v1/sources/{src['id']}/consent", json=body, headers=idem(auth("viewer", sub="user-a"))
    )
    assert r.status_code == 422 and r.json()["code"] == "validation_error", r.text
    assert r.json()["details"]["fouten"][0]["veld"] == "expires_at"
    # met tijdzone (…Z) wel geldig
    r = paired_client.post(
        f"/search/v1/sources/{src['id']}/consent",
        json={**body, "expires_at": "2099-01-01T00:00:00Z"},
        headers=idem(auth("viewer", sub="user-a")),
    )
    assert r.status_code == 200, r.text
    assert r.json()["consent"]["expires_at"] == "2099-01-01T00:00:00Z"


@pytest.mark.parametrize(
    ("body", "veld"),
    [
        ({"name": "   "}, "name"),
        ({"name": "Docs", "owner_sub": "   "}, "owner_sub"),
    ],
)
def test_blank_name_or_owner_is_rejected(
    paired_client: Any, auth: Auth, shares: Shares, body: dict[str, str], veld: str
) -> None:
    r = paired_client.post(
        "/search/v1/sources",
        json={"kind": "files", "allowed_paths": [str(shares.a)], **body},
        headers=idem(auth("admin", sub="beheer")),
    )
    assert r.status_code == 422 and r.json()["code"] == "validation_error", r.text
    assert r.json()["details"]["fouten"][0]["veld"] == veld
    assert paired_client.get("/search/v1/sources", headers=auth("admin", sub="beheer")).json()["items"] == []


def test_blank_patch_name_and_access_principal_are_rejected(paired_client: Any, auth: Auth, shares: Shares) -> None:
    admin = auth("admin", sub="beheer")
    src = make_source(paired_client, auth, str(shares.a), name="  Docs  ")
    assert src["name"] == "Docs"  # witruimte eromheen weg
    r = paired_client.patch(
        f"/search/v1/sources/{src['id']}", json={"name": " "}, headers=idem(admin, **{"If-Match": src["etag"]})
    )
    assert r.status_code == 422, r.text
    r = paired_client.post(
        f"/search/v1/sources/{src['id']}/access",
        json={"principal_type": "user", "principal_id": "  "},
        headers=idem(admin),
    )
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("value", [True, False, "5", 1.5, -1, [1]])
def test_cursor_must_be_a_plain_non_negative_integer(paired_client: Any, auth: Auth, value: Any) -> None:
    admin = auth("admin", sub="beheer")
    cur = _cursor(value)
    for path, params in (
        ("/search/v1/sources", {}),
        ("/search/v1/search", {"q": "x"}),
        ("/connector/v1/events", {}),
        ("/connector/v1/items", {"type": "search.source"}),
    ):
        r = paired_client.get(path, params={**params, "cursor": cur}, headers=admin)
        assert r.status_code == 422 and r.json()["code"] == "validation_error", (path, value, r.text)
        assert r.json()["details"]["fouten"][0]["veld"] == "cursor"


# ------------------------------------------------------------------ onbekend is nooit groen
def test_hub_without_keys_in_enroll_answer_is_never_trusted(app_factory: Callable[..., Any], fake_hub: FakeHub) -> None:
    """Contract 1.2: een Hub die bij het koppelen geen JWKS meegeeft, levert een ongeldig antwoord op — de dienst slaat
    niets op en haalt de sleutels ook niet alsnog via HTTP op (een JWKS via HTTP voegt nooit vertrouwen toe)."""

    def without_jwks(request: httpx.Request) -> httpx.Response | None:
        if request.url.path != "/api/v1/services/enroll":
            return None
        fake_hub.override = None
        resp = fake_hub.handler(request)
        data = json.loads(resp.content)
        data.pop("jwks")
        return httpx.Response(200, json=data)

    fake_hub.override = without_jwks
    client = app_factory()
    out = client.app.state.enrollment.enroll_with_code(HUB_URL, PAIR_CODE, fake_hub.fingerprint())
    assert not out.ok and out.code == "hub_antwoord_ongeldig"
    doc = client.app.state.hub_link.read()
    assert doc["state"] == "nooit_gekoppeld" and doc["service_token"] is None and doc["pins"] is None
    assert not any(r["path"] == "/api/v1/auth/jwks" for r in fake_hub.all_requests)
    # niet gekoppeld → elke Hub-JWT geweigerd; de statuspagina toont 'nog niet gekoppeld'
    r = client.get("/connector/v1/health", headers=fake_hub.service())
    assert r.status_code == 401 and r.json()["details"]["reason"] == "not_linked"


# ------------------------------------------------------------------ kinderprofiel als 'eigenaar'
def test_kid_as_owner_never_gets_admin_view(paired_client: Any, auth: Auth, shares: Shares) -> None:
    client = paired_client
    src = make_source(client, auth, str(shares.a), owner="kind-1", kid_allowed=True, classification="internal")
    # toestemming gegeven toen 'kind-1' nog geen kinderprofiel had; daarna kreeg het account de rol 'kind'
    assert consent(client, auth, src["id"], sub="kind-1").status_code == 200
    kid = auth("kind", sub="kind-1")
    for item in client.get("/search/v1/sources", headers=kid).json()["items"]:
        assert "allowed_paths" not in item and "access" not in item and "owner_sub" not in item
    detail = client.get(f"/search/v1/sources/{src['id']}", headers=kid)
    assert detail.status_code == 200
    assert "allowed_paths" not in detail.json() and "access" not in detail.json() and "consent" not in detail.json()
    for suffix in ("/preview", "/runs"):
        assert client.get(f"/search/v1/sources/{src['id']}{suffix}", headers=kid).status_code == 404
