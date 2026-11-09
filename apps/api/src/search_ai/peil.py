"""Statuspagina-sjabloon van de ontwerptaal **Peil** (ONTWERPTAAL §13.2, O-14).

De bron van waarheid is ``ai-server-manager/design/ontwerptaal/statuspagina.html``; deze repo heeft een versie-gepinde
kopie in ``design/ontwerptaal/`` met een herkomstkop van twee regels (versie, bron-commit, sha256 van het bronbestand).
De rest van het bestand is byte-gelijk aan de bron. ``load_template`` controleert bij het laden dat de inhoud nog
precies die sha256 heeft (een met de hand gewijzigde kopie wordt niet gebruikt); CI vergelijkt de kopie daarnaast met
de bron zelf (``tests/unit/test_peil_kopie.py``).

``render`` doet hetzelfde als de referentie-renderer ``bouw/sjabloon.mjs`` van de ontwerptaal:
``{{x}}`` = tekst (altijd HTML-escaped) · ``<!--ALS:x-->…<!--/ALS:x-->`` = tonen als x waar is ·
``<!--NIET:x-->…<!--/NIET:x-->`` = tonen als x onwaar is · ``<!--ELK:lijst-->…<!--/ELK:lijst-->`` = per item herhalen.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TEMPLATE_NAME = "statuspagina.html"
CSS_NAME = "statuspagina.css"
PEIL_VERSION = "1.1.0"

HEADER = re.compile(
    r"HERKOMST — Peil (?P<version>\d+\.\d+\.\d+) · bron: (?P<repo>\S+) (?P<path>\S+) @ (?P<commit>[0-9a-f]{40}) · "
    r"sha256 van het bronbestand \(zonder deze kop\): (?P<sha256>[0-9a-f]{64})"
)
_ELK = re.compile(r"<!--ELK:(\w+)-->([\s\S]*?)<!--/ELK:\1-->")
_ALS = re.compile(r"<!--ALS:(\w+)-->([\s\S]*?)<!--/ALS:\1-->")
_NIET = re.compile(r"<!--NIET:(\w+)-->([\s\S]*?)<!--/NIET:\1-->")
_PLEK = re.compile(r"\{\{(\w+)\}\}")
# Na het vullen staat er alleen nog uitleg-commentaar in (alle plekken zijn verwerkt en ingevulde tekst is escaped,
# dus bevat geen "<!--"): dat gaat niet mee naar de browser.
_COMMENT = re.compile(r"<!--[\s\S]*?-->\n?")


class PeilTemplateError(RuntimeError):
    """Sjabloon ontbreekt, heeft geen geldige herkomstkop of is na het kopiëren gewijzigd."""


@dataclass(frozen=True)
class Provenance:
    version: str
    repo: str
    path: str
    commit: str
    sha256: str


@dataclass(frozen=True)
class PeilTemplate:
    provenance: Provenance
    body: str  # byte-gelijk aan het bronbestand (zonder herkomstkop)


def default_dir() -> Path:
    """Map met de kopie in de bronboom (``apps/api/src/search_ai/peil.py`` → ``<repo>/design/ontwerptaal``). In de
    image staat de kopie elders; daar zet de Dockerfile ``SEARCH_AI_PEIL_DIR`` (``Settings.peil_dir``)."""
    return Path(__file__).resolve().parents[4] / "design" / "ontwerptaal"


def split_copy(raw: bytes) -> tuple[Provenance, bytes]:
    """Splitst een kopie in (herkomst, inhoud). De kop is precies twee regels; de inhoud is de rest, byte voor byte."""
    lines = raw.split(b"\n", 2)
    if len(lines) != 3:
        raise PeilTemplateError("kopie zonder herkomstkop van twee regels")
    m = HEADER.search(lines[0].decode("utf-8", errors="strict"))
    if not m:
        raise PeilTemplateError("herkomstkop onleesbaar")
    return Provenance(**m.groupdict()), lines[2]


def verify_copy(raw: bytes) -> tuple[Provenance, bytes]:
    prov, body = split_copy(raw)
    actual = hashlib.sha256(body).hexdigest()
    if actual != prov.sha256:
        raise PeilTemplateError(
            f"inhoud wijkt af van de herkomstkop (sha256 {actual[:16]}…, verwacht {prov.sha256[:16]}…)"
        )
    return prov, body


def load_template(directory: Path | None = None) -> PeilTemplate:
    path = (directory or default_dir()) / TEMPLATE_NAME
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PeilTemplateError(f"sjabloon niet gevonden: {path}") from exc
    prov, body = verify_copy(raw)
    if prov.version != PEIL_VERSION:
        raise PeilTemplateError(f"sjabloonversie {prov.version}, verwacht {PEIL_VERSION}")
    return PeilTemplate(prov, body.decode("utf-8"))


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def render(template: str, data: dict[str, Any]) -> str:
    """Vult het sjabloon. Volgorde zoals de referentie: eerst ELK, dan ALS, dan NIET, dan de tekstplekken."""

    def blokken(t: str, d: dict[str, Any]) -> str:
        t = _ELK.sub(lambda m: "".join(blokken(m.group(2), {**d, **item}) for item in (d.get(m.group(1)) or [])), t)
        t = _ALS.sub(lambda m: blokken(m.group(2), d) if d.get(m.group(1)) else "", t)
        t = _NIET.sub(lambda m: "" if d.get(m.group(1)) else blokken(m.group(2), d), t)
        return _PLEK.sub(lambda m: _esc(d.get(m.group(1))), t)

    return blokken(template, data)


def strip_comments(rendered: str) -> str:
    """Verwijdert het uitleg-commentaar uit een **gevulde** pagina (nooit vóór het vullen: de uitleg in het sjabloon
    noemt zelf de plekken, zoals de referentie-renderer die ook gewoon verwerkt)."""
    return _COMMENT.sub("", rendered)
