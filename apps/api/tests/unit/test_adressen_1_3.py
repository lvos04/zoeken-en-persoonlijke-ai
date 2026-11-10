"""Contract 1.3 §3.2/§3.3 (C1.3-ZPA-08): canonieke IP-vormen, IPv6 alleen tussen ``[]``, punt aan het eind van een naam,
verboden bereiken ook na DNS-resolutie bij elke verbinding, en ``base_url`` alleen als hij geldig is."""

from __future__ import annotations

import socket
from typing import Any

import httpcore
import pytest

from search_ai.config import Settings
from search_ai.hubclient import (
    GuardedBackend,
    GuardedTransport,
    HubClient,
    HubUrlError,
    validate_base_url,
    validate_hub_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.1:30400",  # verkorte IPv4-vorm (zou 127.0.0.1 worden)
        "http://2130706433:30400",  # decimaal getal
        "http://0x7f000001:30400",  # hexadecimaal
        "http://0x7f.0.0.1:30400",
        "http://010.0.0.1:30400",  # voorloopnul (octaal in inet_aton)
        "http://0:30400",
        "http://192.168.1.256:30400",
        "http://192.168.1:30400",
        "http://1.2.3.4.5:30400",
        "http://a..b:30400",
        "http://nas..:30400",
        "http://[fe80::1%25eth0]:30400",  # zone-ID
        "http://[nas]:30400",
        "http://na$s:30400",
        "http://" + "a" * 500 + ".lan:1",  # langer dan 512 tekens
    ],
)
def test_niet_canonieke_of_ongeldige_hosts_worden_geweigerd(url: str) -> None:
    with pytest.raises(HubUrlError):
        validate_hub_url(url)


@pytest.mark.parametrize(
    ("url", "verwacht"),
    [
        ("http://192.168.1.20:30400", "http://192.168.1.20:30400"),
        ("http://nas.local.:30400", "http://nas.local:30400"),  # één punt aan het eind telt niet mee
        ("http://NAS.LAN:30400/", "http://nas.lan:30400"),
        ("http://[FD00::10]:30400", "http://[fd00::10]:30400"),
        ("http://cafe:30400", "http://cafe:30400"),  # alleen hex-letters zonder 0x is een naam
        ("http://0.0.0.1:30400", "http://0.0.0.1:30400"),  # canoniek, niet 'alle adressen'
    ],
)
def test_canonieke_vormen_en_namen(url: str, verwacht: str) -> None:
    assert validate_hub_url(url) == verwacht


def test_localhost_met_punt_aan_het_eind_is_ook_loopback() -> None:
    with pytest.raises(HubUrlError, match="Loopback"):
        validate_hub_url("http://localhost.:30400")
    assert validate_hub_url("http://localhost.:30400", allow_loopback=True) == "http://localhost:30400"


def test_hub_adres_tot_512_tekens() -> None:
    host = "n" * 60
    url = "http://" + ".".join([host] * 4) + ".lan:30400"  # geldig en ruim onder 512
    assert validate_hub_url(url).startswith("http://")
    with pytest.raises(HubUrlError, match="512"):
        validate_hub_url("http://nas.lan:30400" + " " * 0 + "/" + "a" * 600)


@pytest.mark.parametrize(
    ("url", "verwacht"),
    [
        ("http://192.168.1.5:30450", "http://192.168.1.5:30450"),
        ("http://192.168.1.5:30450/", "http://192.168.1.5:30450"),
        ("https://nas.lan/zoeken/", "https://nas.lan/zoeken"),  # pad mag bij base_url
        (None, None),
        ("http://127.0.0.1:30450", None),  # loopback: nooit het adres waarop de Hub de dienst bereikt
        ("http://127.1:30450", None),
        ("http://169.254.1.1", None),
        ("ftp://nas.lan", None),
        ("http://nas.lan?x=1", None),
        ("http://user@nas.lan", None),
        ("http://nas.lan/" + "a" * 520, None),
        ("geen adres", None),
    ],
)
def test_base_url_alleen_als_hij_geldig_is(url: Any, verwacht: str | None) -> None:
    """§3.3: de dienst stuurt ``base_url`` alleen als hij geldig is, anders ``null`` (nooit een weigering)."""
    assert validate_base_url(url) == verwacht


# ------------------------------------------------------------------ verboden bereiken na DNS-resolutie
def _resolver(*adressen: str) -> Any:
    def resolve(host: str, port: int, type: int = 0) -> list[tuple[Any, ...]]:
        out = []
        for a in adressen:
            fam = socket.AF_INET6 if ":" in a else socket.AF_INET
            out.append((fam, socket.SOCK_STREAM, 6, "", (a, port)))
        return out

    return resolve


@pytest.mark.parametrize(
    "adressen",
    [
        ("127.0.0.1",),
        ("192.168.1.20", "127.0.0.1"),  # één verboden adres is genoeg om te weigeren
        ("169.254.169.254",),
        ("::1",),
        ("fe80::1%eth0",),
        ("0.0.0.0",),  # noqa: S104 - adres in een test, geen binding
        ("::ffff:127.0.0.1",),
        ("224.0.0.1",),
    ],
)
def test_naam_die_naar_een_verboden_adres_wijst_wordt_niet_verbonden(adressen: tuple[str, ...]) -> None:
    backend = GuardedBackend(resolver=_resolver(*adressen))
    with pytest.raises(httpcore.ConnectError):
        backend.addresses("hub.lan", 30400)
    with pytest.raises(httpcore.ConnectError):
        backend.connect_tcp("hub.lan", 30400, timeout=0.1)


def test_naam_met_toegestane_adressen_verbindt_met_het_gecontroleerde_adres(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = GuardedBackend(resolver=_resolver("192.168.1.20", "192.168.1.20", "fd00::10"))
    assert backend.addresses("hub.lan", 30400) == ["192.168.1.20", "fd00::10"]
    gezien: list[str] = []

    def fake_connect(self: Any, host: str, port: int, *a: Any, **kw: Any) -> Any:
        gezien.append(host)
        raise httpcore.ConnectError("geen netwerk in de test")

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", fake_connect)
    with pytest.raises(httpcore.ConnectError):
        backend.connect_tcp("hub.lan", 30400, timeout=0.1)
    assert gezien == ["192.168.1.20", "fd00::10"]  # nooit opnieuw de naam: alleen gecontroleerde adressen


def test_loopback_via_dns_alleen_met_de_ontwikkelvlag() -> None:
    assert GuardedBackend(allow_loopback=True, resolver=_resolver("127.0.0.1")).addresses("hub", 1) == ["127.0.0.1"]
    with pytest.raises(httpcore.ConnectError):
        GuardedBackend(allow_loopback=True, resolver=_resolver("169.254.1.1")).addresses("hub", 1)


def test_naam_niet_gevonden_is_een_verbindingsfout() -> None:
    def kapot(*_a: Any, **_k: Any) -> Any:
        raise socket.gaierror("niet gevonden")

    with pytest.raises(httpcore.ConnectError):
        GuardedBackend(resolver=kapot).addresses("bestaat-niet.lan", 1)


def test_hubclient_gebruikt_standaard_de_bewaakte_transport() -> None:
    client = HubClient(Settings())._client("http://192.168.1.20:30400")
    try:
        transport = client._transport
        assert isinstance(transport, GuardedTransport)
        assert isinstance(transport._pool._network_backend, GuardedBackend)
    finally:
        client.close()
