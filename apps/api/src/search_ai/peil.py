"""Statuspagina-sjabloon van de ontwerptaal **Peil** (ONTWERPTAAL §13.2 en §14, O-14, E07/E08).

De bron van waarheid is ``ai-server-manager/design/ontwerptaal/statuspagina.html``; deze repo heeft een versie-gepinde
kopie in ``design/ontwerptaal/`` met een herkomstkop van twee regels (versie, bron-commit, sha256 van het bronbestand).
De rest van het bestand is byte-gelijk aan de bron. ``load_template`` controleert bij het laden dat de inhoud nog
precies die sha256 heeft en de verwachte versie is (een met de hand gewijzigde kopie wordt niet gebruikt); CI
vergelijkt de kopie daarnaast met de bron zelf (``tests/unit/test_peil_kopie.py``, pin- en driftregel E08).

``render`` vult in **één doorgang** (Peil 1.1.1, E07), gelijk aan de referentie ``bouw/sjabloon.py``/``.mjs``: het
sjabloon wordt eerst in een boom gelezen en daarna uitgeschreven; een ingevulde waarde wordt nooit opnieuw als sjabloon
gelezen. ``{{x}}`` = tekst (altijd HTML-escaped) · ``<!--ALS:x-->…<!--/ALS:x-->`` = tonen als x waar is ·
``<!--NIET:x-->…<!--/NIET:x-->`` = tonen als x onwaar is · ``<!--ELK:lijst-->…<!--/ELK:lijst-->`` = per item herhalen;
blokken mogen genest zijn. Waar = niet leeg (``False``, ``None``, ``""``, 0 en een lege lijst zijn onwaar).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TEMPLATE_NAME = "statuspagina.html"
CSS_NAME = "statuspagina.css"
PEIL_VERSION = "1.1.1"

HEADER = re.compile(
    r"HERKOMST — Peil (?P<version>\d+\.\d+\.\d+) · bron: (?P<repo>\S+) (?P<path>\S+) @ (?P<commit>[0-9a-f]{40}) · "
    r"sha256 van het bronbestand \(zonder deze kop\): (?P<sha256>[0-9a-f]{64})"
)
_TOKEN = re.compile(r"<!--(/?)(ELK|ALS|NIET):(\w+)-->|\{\{(\w+)\}\}")
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
    text = body.decode("utf-8")
    parse(text)  # een sjabloon dat niet goed genest is, wordt bij het laden geweigerd (niet pas bij het tonen)
    return PeilTemplate(prov, text)


def _esc(value: Any) -> str:
    """Zoals de referentie: ``&<>"'`` → entiteiten; ``True``/``False`` als ``true``/``false`` (JavaScript)."""
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


Node = str | dict[str, Any]


def parse(template: str) -> list[Node]:
    """Leest het sjabloon in een boom; ``PeilTemplateError`` bij een niet-gesloten of verkeerd genest blok."""
    root: dict[str, Any] = {"kids": []}
    stack = [root]
    pos = 0
    for m in _TOKEN.finditer(template):
        top = stack[-1]
        if m.start() > pos:
            top["kids"].append(template[pos : m.start()])
        pos = m.end()
        if m.group(4):
            top["kids"].append({"plek": m.group(4)})
        elif not m.group(1):
            block: dict[str, Any] = {"soort": m.group(2), "naam": m.group(3), "kids": []}
            top["kids"].append(block)
            stack.append(block)
        else:
            if len(stack) == 1 or top["soort"] != m.group(2) or top["naam"] != m.group(3):
                raise PeilTemplateError(f"sjabloon: onverwachte sluiting {m.group(0)}")
            stack.pop()
    if len(stack) != 1:
        raise PeilTemplateError(f"sjabloon: blok niet gesloten: {stack[-1]['soort']}:{stack[-1]['naam']}")
    if pos < len(template):
        root["kids"].append(template[pos:])
    return list(root["kids"])


def _write(nodes: list[Node], data: dict[str, Any]) -> str:
    out: list[str] = []
    for node in nodes:
        if isinstance(node, str):
            out.append(node)
        elif "plek" in node:
            out.append(_esc(data.get(node["plek"])))
        elif node["soort"] == "ELK":
            for item in data.get(node["naam"]) or []:
                out.append(_write(node["kids"], {**data, **item}))
        elif node["soort"] == "ALS":
            if data.get(node["naam"]):
                out.append(_write(node["kids"], data))
        elif not data.get(node["naam"]):  # NIET
            out.append(_write(node["kids"], data))
    return "".join(out)


def render(template: str, data: dict[str, Any]) -> str:
    """Vult het sjabloon in één doorgang (E07): een waarde wordt nooit opnieuw als sjabloon gelezen."""
    return _write(parse(template), data)


def strip_comments(rendered: str) -> str:
    """Verwijdert het uitleg-commentaar uit een **gevulde** pagina (nooit vóór het vullen: de uitleg in het sjabloon
    noemt zelf de plekken, zoals de referentie-renderer die ook gewoon verwerkt)."""
    return _COMMENT.sub("", rendered)
