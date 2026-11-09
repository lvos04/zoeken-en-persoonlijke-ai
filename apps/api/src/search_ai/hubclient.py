"""Uitgaande HTTP-calls naar de Hub (contract §5: 5 s connect / 15 s read, retry met backoff + jitter, max 3).

Dit is de **enige** uitgaande verbinding van deze dienst in P0: alleen naar de Hub (koppelen, JWKS, events). Geen
zoekinhoud, geen documenten en geen embeddings verlaten de NAS (B07-001).
"""

from __future__ import annotations

import ipaddress
import random
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import __version__
from .config import Settings

RETRY_STATUS = {429, 502, 503, 504}
REDIRECT = range(300, 400)  # nooit volgen: 3xx = storing (contract 1.2 §3.2)


class HubUrlError(ValueError):
    """Ongeldig adres; de boodschap is Nederlands en mag aan de gebruiker getoond worden."""


def _bad_ip(host: str, allow_loopback: bool) -> str | None:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
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


def validate_hub_url(url: str, allow_loopback: bool = False, label: str = "Het Hub-adres") -> str:
    """Regels van contract 1.2 §3.2 voor ``HUB_URL`` én het Hub-adres op de statuspagina.

    ``http(s)://<host>[:<poort>]`` zonder pad, query, fragment of inloggegevens; geen loopback (alleen met de
    ontwikkelvlag), geen link-local (169.254.0.0/16, fe80::/10), geen 0.0.0.0/:: en geen multicast. Geeft het adres
    zonder afsluitende ``/`` terug.
    """
    if not isinstance(url, str):
        raise HubUrlError(f"{label} is ongeldig.")
    url = url.strip()
    if len(url) > 500:
        raise HubUrlError(f"{label} is te lang.")
    if any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise HubUrlError(f"{label} bevat spaties of stuurtekens.")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise HubUrlError(f"{label} moet met http:// of https:// beginnen.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise HubUrlError("Zet geen gebruikersnaam of wachtwoord in het adres.")
    if not parts.hostname:
        raise HubUrlError(f"{label} mist een hostnaam of IP-adres.")
    if parts.query or parts.fragment or "?" in url or "#" in url:
        raise HubUrlError(f"{label} mag geen ?-parameters of #-deel bevatten.")
    if parts.path not in ("", "/"):
        raise HubUrlError(f"{label} mag geen pad bevatten: alleen http(s)://<NAS-adres>:<poort>.")
    try:
        port = parts.port
    except ValueError as exc:
        raise HubUrlError(f"{label} heeft een ongeldige poort.") from exc
    if port == 0:
        raise HubUrlError(f"{label} heeft een ongeldige poort.")
    host = parts.hostname.lower()
    named_loopback = host == "localhost" or host.endswith(".localhost")
    problem = "loopback" if named_loopback and not allow_loopback else _bad_ip(host, allow_loopback)
    if problem == "loopback":
        raise HubUrlError(
            "Loopback-adressen (localhost/127.0.0.1) wijzen naar deze container zelf. Gebruik het LAN-adres van de NAS "
            "met de poort van de Hub."
        )
    if problem is not None:
        raise HubUrlError(
            f"{label} mag geen link-local-, multicast- of 'alle adressen'-adres zijn (169.254.x.x, fe80::, 0.0.0.0, ::)."
        )
    return f"{parts.scheme}://{parts.netloc}"


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
            transport=self.transport,
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
