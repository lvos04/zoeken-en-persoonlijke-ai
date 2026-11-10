"""Uitgaande HTTP-calls naar de Hub (contract §5: 5 s connect / 15 s read, retry met backoff + jitter, max 3).

Dit is de **enige** uitgaande verbinding van deze dienst in P0: alleen naar de Hub (koppelen, sleutelrotaties, events).
Geen zoekinhoud, geen documenten en geen embeddings verlaten de NAS (B07-001).

Adresregels (contract 1.3 §3.2): IPv4 alleen als ``a.b.c.d`` (vier decimale delen 0–255, zonder voorloopnul); een host
van alleen cijfers, punten en hex-delen die daar niet aan voldoet (``127.1``, ``2130706433``, ``0x7f000001``,
``010.0.0.1``, ``0``) is ongeldig; IPv6 alleen tussen ``[]``; één punt aan het eind van een naam telt niet mee. Bij een
hostnaam gelden de verboden bereiken (loopback, link-local, ``0.0.0.0``/``::``, multicast) ook voor het **opgeloste**
adres, bij **elke** verbinding: ``GuardedBackend`` lost de naam zelf op, controleert elk adres en verbindt alleen met een
gecontroleerd adres (geen tweede DNS-vraag tussen controle en verbinding).
"""

from __future__ import annotations

import ipaddress
import random
import re
import socket
import time
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx

from . import __version__
from .config import Settings

RETRY_STATUS = {429, 502, 503, 504}
REDIRECT = range(300, 400)  # nooit volgen: 3xx = storing (contract §3.2)
MAX_URL_LEN = 512  # contract 1.3 §3.3 (base_url) en §3.7 (Hub-adres op de statuspagina)
_NUMERIC_LABEL = re.compile(r"(?:[0-9]+|0x[0-9a-f]*)")
_CANONICAL_OCTET = re.compile(r"(?:0|[1-9][0-9]{0,2})")
_NAME_LABEL = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?")

Address = ipaddress.IPv4Address | ipaddress.IPv6Address


class HubUrlError(ValueError):
    """Ongeldig adres; de boodschap is Nederlands en mag aan de gebruiker getoond worden."""


def forbidden_ip(ip: Address, allow_loopback: bool = False) -> str | None:
    """Reden waarom een (opgelost) adres verboden is, of ``None``. IPv4 in IPv6 (``::ffff:a.b.c.d``) telt als IPv4."""
    mapped = getattr(ip, "ipv4_mapped", None)
    for addr in (ip, mapped) if mapped is not None else (ip,):
        if addr.is_unspecified:
            return "unspecified"
        if addr.is_loopback and not allow_loopback:
            return "loopback"
        if addr.is_link_local:
            return "link_local"
        if addr.is_multicast:
            return "multicast"
    return None


def canonical_host(raw: str, bracketed: bool) -> tuple[str, Address | None]:
    """Host volgens de vormregels van §3.2 → (host zonder afsluitende punt, IP of ``None`` bij een naam).

    ``HubUrlError`` bij een niet-canonieke IP-vorm, IPv6 zonder ``[]``, een zone-ID of een ongeldige naam."""
    host = raw.lower()
    if bracketed:
        if "%" in host:
            raise HubUrlError("Een IPv6-adres met een zone (%) is niet toegestaan.")
        try:
            return host, ipaddress.IPv6Address(host)
        except ValueError as exc:
            raise HubUrlError("Tussen [ ] hoort een geldig IPv6-adres.") from exc
    if ":" in host:
        raise HubUrlError("Zet een IPv6-adres tussen [ ], bijvoorbeeld http://[fd00::10]:30400.")
    if host.endswith("."):
        host = host[:-1]  # één punt aan het eind van een naam telt niet mee
    labels = host.split(".")
    if not host or any(not label for label in labels):
        raise HubUrlError("Het adres heeft een lege naam of twee punten achter elkaar.")
    if all(_NUMERIC_LABEL.fullmatch(label) for label in labels):
        # alleen cijfers, punten en hex-delen: dan uitsluitend de canonieke vorm a.b.c.d (geen 127.1, 0x7f000001, 010.…)
        if len(labels) != 4 or not all(_CANONICAL_OCTET.fullmatch(x) and int(x) <= 255 for x in labels):
            raise HubUrlError("Een IPv4-adres alleen als a.b.c.d (vier getallen 0–255, zonder voorloopnul).")
        return host, ipaddress.IPv4Address(host)
    if len(host) > 253 or not all(_NAME_LABEL.fullmatch(label) for label in labels):
        raise HubUrlError("De hostnaam bevat ongeldige tekens.")
    return host, None


def _problem_text(problem: str, label: str) -> str:
    if problem == "loopback":
        return (
            "Loopback-adressen (localhost/127.0.0.1) wijzen naar deze container zelf. Gebruik het LAN-adres van de NAS "
            "met de poort van de Hub."
        )
    return f"{label} mag geen link-local-, multicast- of 'alle adressen'-adres zijn (169.254.x.x, fe80::, 0.0.0.0, ::)."


def _parse(url: Any, allow_loopback: bool, label: str, *, allow_path: bool) -> tuple[str, str, str, int | None, str]:
    """Gedeelde controle van ``validate_hub_url`` en ``validate_base_url`` → (schema, host, hostdeel, poort, pad)."""
    if not isinstance(url, str):
        raise HubUrlError(f"{label} is ongeldig.")
    url = url.strip()
    if len(url) > MAX_URL_LEN:
        raise HubUrlError(f"{label} is te lang (hooguit {MAX_URL_LEN} tekens).")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise HubUrlError(f"{label} bevat spaties of stuurtekens.")
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
    except ValueError as exc:  # bv. iets anders dan IPv6 tussen [ ]
        raise HubUrlError(f"{label} is ongeldig.") from exc
    if parts.scheme not in ("http", "https"):
        raise HubUrlError(f"{label} moet met http:// of https:// beginnen.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise HubUrlError("Zet geen gebruikersnaam of wachtwoord in het adres.")
    if not hostname:
        raise HubUrlError(f"{label} mist een hostnaam of IP-adres.")
    if parts.query or parts.fragment or "?" in url or "#" in url:
        raise HubUrlError(f"{label} mag geen ?-parameters of #-deel bevatten.")
    if not allow_path and parts.path not in ("", "/"):
        raise HubUrlError(f"{label} mag geen pad bevatten: alleen http(s)://<NAS-adres>:<poort>.")
    try:
        port = parts.port
    except ValueError as exc:
        raise HubUrlError(f"{label} heeft een ongeldige poort.") from exc
    if port == 0:
        raise HubUrlError(f"{label} heeft een ongeldige poort.")
    bracketed = parts.netloc.rsplit("@", 1)[-1].startswith("[")
    host, ip = canonical_host(hostname, bracketed)
    named_loopback = ip is None and (host == "localhost" or host.endswith(".localhost"))
    problem = (
        "loopback" if named_loopback and not allow_loopback else (forbidden_ip(ip, allow_loopback) if ip else None)
    )
    if problem is not None:
        raise HubUrlError(_problem_text(problem, label))
    hostpart = f"[{host}]" if bracketed else host
    return parts.scheme, host, hostpart, port, parts.path


def validate_hub_url(url: Any, allow_loopback: bool = False, label: str = "Het Hub-adres") -> str:
    """Regels van contract 1.3 §3.2 voor ``HUB_URL`` én het Hub-adres op de statuspagina.

    ``http(s)://<host>[:<poort>]`` zonder pad, query, fragment of inloggegevens, hooguit 512 tekens; IP-adressen alleen
    in de canonieke vorm; geen loopback (alleen met de ontwikkelvlag), geen link-local (169.254.0.0/16, fe80::/10), geen
    0.0.0.0/:: en geen multicast. Geeft ``schema://host[:poort]`` terug (host in kleine letters, zonder afsluitende punt).
    """
    scheme, _host, hostpart, port, _path = _parse(url, allow_loopback, label, allow_path=False)
    return f"{scheme}://{hostpart}" + (f":{port}" if port is not None else "")


def validate_base_url(url: Any, allow_loopback: bool = False) -> str | None:
    """``base_url`` voor het enroll-verzoek (§3.3): alleen als hij voldoet aan ``http(s)://host[:poort][/pad]``,
    ≤ 512 tekens en de adresregels van §3.2; anders ``None`` (de dienst stuurt dan ``null``)."""
    if url is None:
        return None
    try:
        scheme, _host, hostpart, port, path = _parse(url, allow_loopback, "Het eigen adres", allow_path=True)
    except HubUrlError:
        return None
    out = f"{scheme}://{hostpart}" + (f":{port}" if port is not None else "") + path.rstrip("/")
    return out if len(out) <= MAX_URL_LEN else None


class GuardedBackend(httpcore.SyncBackend):
    """Netwerklaag die elke verbinding zelf oplost en controleert (§3.2: verboden bereiken ook na DNS-resolutie, bij
    elke verbinding). Verwijst een naam (ook) naar een verboden adres, dan wordt er niet verbonden."""

    def __init__(
        self,
        allow_loopback: bool = False,
        resolver: Callable[..., Iterable[tuple[Any, ...]]] = socket.getaddrinfo,
    ) -> None:
        self.allow_loopback = allow_loopback
        self.resolver = resolver

    def addresses(self, host: str, port: int) -> list[str]:
        try:
            infos = list(self.resolver(host, port, type=socket.SOCK_STREAM))
        except OSError as exc:
            raise httpcore.ConnectError(f"naam niet gevonden: {type(exc).__name__}") from exc
        out: list[str] = []
        for info in infos:
            raw = str(info[4][0])
            try:
                ip = ipaddress.ip_address(raw.split("%", 1)[0])
            except ValueError as exc:
                raise httpcore.ConnectError("onleesbaar adres uit DNS") from exc
            if forbidden_ip(ip, self.allow_loopback) is not None or "%" in raw:
                raise httpcore.ConnectError("de naam verwijst naar een niet-toegestaan adres (contract §3.2)")
            if raw not in out:
                out.append(raw)
        if not out:
            raise httpcore.ConnectError("naam niet gevonden")
        return out

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        last: Exception | None = None
        for addr in self.addresses(host, port):
            try:
                return super().connect_tcp(addr, port, timeout, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last = exc
        assert last is not None  # noqa: S101 - addresses() geeft minstens één adres of een fout
        raise last


class GuardedTransport(httpx.HTTPTransport):
    """``httpx.HTTPTransport`` met ``GuardedBackend`` (TLS-servernaam blijft de hostnaam: httpcore gebruikt de
    oorsprong voor SNI en certificaatcontrole, niet het gekozen IP-adres)."""

    def __init__(self, allow_loopback: bool = False) -> None:
        super().__init__(trust_env=False)
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            max_connections=10,
            max_keepalive_connections=0,
            network_backend=GuardedBackend(allow_loopback),
        )


class HubClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.sleep = sleep

    def _client(self, base_url: str) -> httpx.Client:
        s = self.settings
        timeout = httpx.Timeout(
            connect=s.http_connect_timeout_s,
            read=s.http_read_timeout_s,
            write=s.http_read_timeout_s,
            pool=s.http_connect_timeout_s,
        )
        return httpx.Client(
            base_url=base_url,
            timeout=timeout,
            transport=self.transport or GuardedTransport(s.allow_loopback_hub),
            follow_redirects=False,
            trust_env=False,  # nooit via een proxy uit de omgeving: alleen het LAN-adres van de Hub
            headers={"User-Agent": f"zoeken-en-persoonlijke-ai/{__version__}", "Accept": "application/json"},
        )

    def request(
        self,
        method: str,
        base_url: str,
        path: str,
        *,
        json: Any = None,
        headers: dict[str, str] | None = None,
        attempts: int | None = None,
    ) -> httpx.Response:
        """Voert een call uit met maximaal ``http_max_attempts`` pogingen bij netwerkfouten en 429/5xx-gateway-fouten."""
        max_attempts = attempts or self.settings.http_max_attempts
        last_exc: Exception | None = None
        with self._client(base_url) as client:
            for attempt in range(1, max_attempts + 1):
                try:
                    resp = client.request(method, path, json=json, headers=headers)
                except httpx.TransportError as exc:
                    last_exc = exc
                else:
                    if resp.status_code not in RETRY_STATUS or attempt == max_attempts:
                        return resp
                if attempt < max_attempts:
                    self.sleep(min(8.0, 0.5 * (2 ** (attempt - 1))) * random.uniform(0.5, 1.5))  # noqa: S311
        if last_exc is None:  # pragma: no cover - onbereikbaar: de lus geeft altijd een respons of een fout
            raise RuntimeError("geen respons")
        raise last_exc

    def get_json(self, base_url: str, path: str, headers: dict[str, str] | None = None) -> Any:
        resp = self.request("GET", base_url, path, headers=headers)
        resp.raise_for_status()
        return resp.json()
