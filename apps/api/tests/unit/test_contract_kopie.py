"""Contract 1.3 (kop, §10, C1.3-ALG-01/C1.3-ZPA-12): byte-gelijke kopie van het platformcontract en de conformiteitslijst.

- ``docs/platform-v2/PLATFORM_CONTRACTEN.md`` en ``docs/platform-v2/CONTRACT_1.3_CONFORMITEIT.md`` zijn byte-gelijk aan
  de bron in ``lvos04/ai-server-manager`` op een **vaste bron-commit** (``BRON_COMMIT``). Deze repo past een kopie nooit
  aan; een nieuwe kopie overnemen is een stap van de orkestratie (nieuwe commit + nieuwe sha256 hieronder).
- **Offline** (elke run): de sha256 van beide kopieën is gelijk aan de vastgelegde waarde; de kop noemt ``contracts 1.3``.
- **Tegen de bron:** CI haalt beide bestanden op de bron-commit op (``CONTRACT_BRON_DIR``) en zet
  ``CONTRACT_BRON_VERPLICHT=1``: zonder bron faalt de test dan in plaats van over te slaan. Lokaal wordt de naastgelegen
  checkout ``../ai-server-manager`` gebruikt (``git show <commit>:<pad>``).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT

DOCS = REPO_ROOT / "docs" / "platform-v2"
BRON_REPO = "lvos04/ai-server-manager"
#: commit in ai-server-manager (branch ccr-c133d7a0-kuz1cv) waarop beide bestanden de inhoud hieronder hebben
BRON_COMMIT = "0a7143bc0a94ed5317a9655fa785e73a1fb07ba1"
KOPIEEN = {
    "PLATFORM_CONTRACTEN.md": "15b30cf17f91b606451ef9c41f02327076ef76ce655ee94e40e3538106962310",
    "CONTRACT_1.3_CONFORMITEIT.md": "f0a2352ddb27088ae668013501c62a1eaaf879efe742a2d4422ffe8d33b43466",
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("naam", sorted(KOPIEEN))
def test_kopie_heeft_de_vastgelegde_sha256(naam: str) -> None:
    data = (DOCS / naam).read_bytes()
    assert _sha(data) == KOPIEEN[naam], f"{naam} is lokaal gewijzigd of niet de vastgelegde versie (contract kop)"


def test_kop_noemt_contract_1_3_en_de_eigenaar() -> None:
    kop = (DOCS / "PLATFORM_CONTRACTEN.md").read_text(encoding="utf-8").splitlines()[:4]
    assert any("`contracts 1.3`" in r for r in kop)
    assert any("lvos04/ai-server-manager" in r for r in kop)
    lijst = (DOCS / "CONTRACT_1.3_CONFORMITEIT.md").read_text(encoding="utf-8")
    assert lijst.startswith("# Conformiteit met contract 1.3")
    # de punten van deze repo staan in de lijst die we volgen
    assert re.search(r"\| C1\.3-ZPA-01 \|", lijst) and re.search(r"\| C1\.3-ZPA-14 \|", lijst)


def test_wijziging_in_een_kopie_wordt_herkend(tmp_path: Path) -> None:
    """Negatief: één byte anders → andere sha256 (de test hierboven zou falen)."""
    data = (DOCS / "PLATFORM_CONTRACTEN.md").read_bytes()
    gewijzigd = data.replace(b"`contracts 1.3`", b"`contracts 1.4`", 1)
    assert gewijzigd != data and _sha(gewijzigd) != KOPIEEN["PLATFORM_CONTRACTEN.md"]


def _bron(naam: str) -> bytes | None:
    env = os.environ.get("CONTRACT_BRON_DIR")
    if env:
        return (Path(env) / naam).read_bytes()
    sibling = REPO_ROOT.parent / "ai-server-manager"
    git = shutil.which("git")
    if git and (sibling / ".git").exists():
        r = subprocess.run(  # noqa: S603 - vaste opdracht, geen shell
            [git, "-C", str(sibling), "show", f"{BRON_COMMIT}:docs/platform-v2/{naam}"],
            capture_output=True,
            check=False,
        )
        if r.returncode == 0:
            return r.stdout
    return None


@pytest.mark.parametrize("naam", sorted(KOPIEEN))
def test_geen_drift_met_de_bron_op_de_bron_commit(naam: str) -> None:
    bron = _bron(naam)
    if bron is None:
        if os.environ.get("CONTRACT_BRON_VERPLICHT") == "1":
            pytest.fail(f"bron van {naam} niet beschikbaar (CONTRACT_BRON_DIR)")
        pytest.skip("bron (ai-server-manager) niet beschikbaar; CI controleert dit met CONTRACT_BRON_DIR")
    assert _sha(bron) == KOPIEEN[naam], f"bron {naam} @ {BRON_COMMIT[:12]} heeft een andere sha256"
    assert (DOCS / naam).read_bytes() == bron, f"drift: kopie {naam} wijkt af van de bron"


def test_ci_haalt_de_bron_op_de_vastgelegde_commit_op() -> None:
    """CI leest de commit uit dit bestand (één bron voor de pin) en haalt de bron verplicht op."""
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "s/^BRON_COMMIT = " in ci and "apps/api/tests/unit/test_contract_kopie.py)" in ci
    assert re.search(r"^BRON_COMMIT = \"[0-9a-f]{40}\"$", Path(__file__).read_text(encoding="utf-8"), re.M)
    assert "CONTRACT_BRON_VERPLICHT=1" in ci
    assert "tests/unit/test_contract_kopie.py" in ci
