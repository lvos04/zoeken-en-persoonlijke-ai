"""O-14 / ONTWERPTAAL §14 (Peil 1.1.1, E08): ``design/ontwerptaal/statuspagina.{html,css}`` is een versie-gepinde kopie.

- De herkomstkop (twee regels) noemt versie 1.1.1, de bron-commit in ``lvos04/ai-server-manager`` en de sha256 van het
  bronbestand; de rest van de kopie heeft precies die sha256 (geen handwerk in de kopie).
- **Pin- en driftregel (E08):** CI faalt bij (1) een afwijking van de gepinde inhoud (de kopie ≠ de bron op de gepinde
  commit) en (2) dezelfde versie met andere inhoud op de werkbranch van de bron. Een **nieuwere** bronversie op de
  werkbranch geeft alleen een waarschuwing: bijwerken is een bewuste stap, geen rode CI.
- CI haalt de bron op de gepinde commit op (``PEIL_BRON_DIR``) en de werkbranch (``PEIL_WERKBRANCH_DIR``) en zet
  ``PEIL_BRON_VERPLICHT=1``: zonder bron faalt de test dan in plaats van over te slaan. Lokaal wordt de naastgelegen
  checkout ``../ai-server-manager`` gebruikt (``git show <commit>:<pad>`` en ``git show HEAD:<pad>``).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import warnings
from pathlib import Path

import pytest

from search_ai.peil import PEIL_VERSION, PeilTemplateError, load_template, split_copy, verify_copy
from tests.conftest import REPO_ROOT

KOPIE = REPO_ROOT / "design" / "ontwerptaal"
BESTANDEN = ("statuspagina.html", "statuspagina.css")
BRON_REPO = "lvos04/ai-server-manager"


def _copy(name: str) -> bytes:
    return (KOPIE / name).read_bytes()


@pytest.mark.parametrize("name", BESTANDEN)
def test_kopie_heeft_herkomstkop_en_ongewijzigde_inhoud(name: str) -> None:
    prov, body = verify_copy(_copy(name))
    assert prov.version == PEIL_VERSION == "1.1.1"
    assert prov.repo == BRON_REPO and prov.path == f"design/ontwerptaal/{name}"
    assert hashlib.sha256(body).hexdigest() == prov.sha256
    assert "Versie-gepinde kopie (O-14)" in _copy(name).split(b"\n", 2)[1].decode()


def test_beide_kopieen_komen_van_dezelfde_commit() -> None:
    commits = {split_copy(_copy(n))[0].commit for n in BESTANDEN}
    assert len(commits) == 1


def test_inline_css_in_het_sjabloon_is_statuspagina_css() -> None:
    _, html_body = split_copy(_copy("statuspagina.html"))
    _, css_body = split_copy(_copy("statuspagina.css"))
    m = re.search(rb"<style>(/\*!.*?)</style>", html_body, flags=re.S)
    assert m and m.group(1) == css_body.strip()
    assert b"Peil statuspagina.css 1.1.1" in m.group(1)


@pytest.mark.parametrize("name", BESTANDEN)
def test_handwerk_in_de_kopie_wordt_herkend(name: str) -> None:
    raw = _copy(name)
    head, rest = raw.split(b"\n", 2)[:2], raw.split(b"\n", 2)[2]
    tampered = b"\n".join(head) + b"\n" + rest.replace(b"--background:#EEF1F5", b"--background:#EEF1F6", 1)
    assert tampered != raw
    with pytest.raises(PeilTemplateError, match="wijkt af"):
        verify_copy(tampered)
    with pytest.raises(PeilTemplateError):
        verify_copy(rest)  # zonder herkomstkop


def test_load_template_weigert_gewijzigde_ontbrekende_en_andere_versie(tmp_path: Path) -> None:
    assert load_template(KOPIE).provenance.version == PEIL_VERSION
    with pytest.raises(PeilTemplateError, match="niet gevonden"):
        load_template(tmp_path)
    raw = _copy("statuspagina.html")
    (tmp_path / "statuspagina.html").write_bytes(raw.replace(b"<h1>{{dienst_naam}}</h1>", b"<h1>x</h1>"))
    with pytest.raises(PeilTemplateError, match="wijkt af"):
        load_template(tmp_path)
    (tmp_path / "statuspagina.html").write_bytes(
        raw.replace(b"Peil 1.1.1 \xc2\xb7 bron", b"Peil 1.0.0 \xc2\xb7 bron", 1)
    )
    with pytest.raises(PeilTemplateError, match=r"versie 1\.0\.0"):
        load_template(tmp_path)


def _bron(name: str, commit: str, env_var: str = "PEIL_BRON_DIR") -> bytes | None:
    env = os.environ.get(env_var)
    if env:
        return (Path(env) / name).read_bytes()
    sibling = REPO_ROOT.parent / "ai-server-manager"
    git = shutil.which("git")
    if git and (sibling / ".git").exists():
        r = subprocess.run(
            [git, "-C", str(sibling), "show", f"{commit}:design/ontwerptaal/{name}"],
            capture_output=True,
            check=False,
        )
        if r.returncode == 0:
            return r.stdout
    return None


@pytest.mark.parametrize("name", BESTANDEN)
def test_geen_drift_met_de_bron_op_de_gepinde_commit(name: str) -> None:
    prov, body = split_copy(_copy(name))
    bron = _bron(name, prov.commit)
    if bron is None:
        if os.environ.get("PEIL_BRON_VERPLICHT") == "1":
            pytest.fail(f"bron van {name} niet beschikbaar (PEIL_BRON_DIR)")
        pytest.skip("bron (ai-server-manager) niet beschikbaar; CI controleert dit met PEIL_BRON_DIR")
    assert hashlib.sha256(bron).hexdigest() == prov.sha256, f"bron {name} @ {prov.commit[:12]} heeft een andere sha256"
    assert body == bron, f"drift: kopie {name} wijkt af van de bron"


BRONVERSIE = {
    "statuspagina.html": re.compile(rb"<!-- Peil statuspagina-sjabloon (\d+\.\d+\.\d+) \xc2\xb7 "),
    "statuspagina.css": re.compile(rb"^/\*! Peil statuspagina\.css (\d+\.\d+\.\d+) \xc2\xb7 "),
}


def _versie(name: str, data: bytes) -> tuple[int, ...]:
    m = BRONVERSIE[name].search(data[:800])
    assert m, f"{name}: versie van de bron onleesbaar"
    return tuple(int(x) for x in m.group(1).decode().split("."))


@pytest.mark.parametrize("name", BESTANDEN)
def test_pinregel_ten_opzichte_van_de_werkbranch_van_de_bron(name: str) -> None:
    """E08: dezelfde versie met andere inhoud op de werkbranch = fout; een nieuwere versie = alleen een waarschuwing."""
    prov, body = split_copy(_copy(name))
    bron = _bron(name, "HEAD", env_var="PEIL_WERKBRANCH_DIR")
    if bron is None:
        if os.environ.get("PEIL_BRON_VERPLICHT") == "1":
            pytest.fail(f"werkbranch-bron van {name} niet beschikbaar (PEIL_WERKBRANCH_DIR)")
        pytest.skip("werkbranch van ai-server-manager niet beschikbaar")
    eigen, daar = _versie(name, body), _versie(name, bron)
    assert eigen == tuple(int(x) for x in prov.version.split("."))
    if daar == eigen:
        assert hashlib.sha256(bron).hexdigest() == prov.sha256, (
            f"drift: {name} {prov.version} heeft op de werkbranch andere inhoud dan de gepinde kopie"
        )
    elif daar > eigen:
        warnings.warn(
            f"Peil {'.'.join(map(str, daar))} staat op de werkbranch; deze kopie pint {prov.version} (bewust bijwerken)",
            stacklevel=1,
        )
    else:
        pytest.fail(f"de werkbranch heeft een oudere Peil-versie ({daar}) dan de gepinde kopie ({eigen})")


def test_pinregel_zelftest_zelfde_versie_andere_inhoud_wordt_herkend() -> None:
    """Negatief: een bron met dezelfde versie en één byte anders geeft een andere sha256 (de test hierboven faalt)."""
    prov, body = split_copy(_copy("statuspagina.css"))
    anders = body.replace(b"--background:#EEF1F5", b"--background:#EEF1F6", 1)
    assert _versie("statuspagina.css", anders) == _versie("statuspagina.css", body)
    assert hashlib.sha256(anders).hexdigest() != prov.sha256
