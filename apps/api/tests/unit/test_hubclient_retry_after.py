"""Contract §5 (QA contract 1.3, ADR-0017): uitgaande calls naar de Hub proberen bij 429 of 503 **nooit eerder**
opnieuw dan ``Retry-After``; vraagt de Hub langer te wachten dan binnen één aanroep redelijk is, dan krijgt de aanroeper
het antwoord direct terug (en plant hij zelf later)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from search_ai.config import Settings
from search_ai.hubclient import MAX_INLINE_WAIT_S, HubClient, retry_after_s

HUB = "http://192.168.1.20:30400"


def _client(responses: list[httpx.Response], sleeps: list[float]) -> tuple[HubClient, list[str]]:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        return responses[min(len(seen) - 1, len(responses) - 1)]

    return HubClient(Settings(), transport=httpx.MockTransport(handler), sleep=sleeps.append), seen


@pytest.mark.parametrize("status", [429, 503])
def test_korte_retry_after_wordt_afgewacht(status: int) -> None:
    sleeps: list[float] = []
    hub, seen = _client([httpx.Response(status, headers={"Retry-After": "3"}), httpx.Response(200, json={})], sleeps)
    assert hub.request("GET", HUB, "/x").status_code == 200
    assert len(seen) == 2 and sleeps and sleeps[0] >= 3.0  # zonder de reparatie: 0,25–0,75 s


@pytest.mark.parametrize("status", [429, 503])
def test_lange_retry_after_geeft_het_antwoord_direct_terug(status: int) -> None:
    sleeps: list[float] = []
    hub, seen = _client([httpx.Response(status, headers={"Retry-After": "120"})], sleeps)
    resp = hub.request("GET", HUB, "/x")
    assert resp.status_code == status and len(seen) == 1 and sleeps == []
    assert retry_after_s(resp) == 120.0


def test_502_zonder_retry_after_houdt_de_gewone_backoff() -> None:
    sleeps: list[float] = []
    hub, seen = _client([httpx.Response(502), httpx.Response(200, json={})], sleeps)
    assert hub.request("GET", HUB, "/x").status_code == 200
    assert len(seen) == 2 and len(sleeps) == 1 and sleeps[0] < 1.0


def test_retry_after_als_http_datum_en_onleesbaar() -> None:
    later = format_datetime(datetime.now(UTC) + timedelta(seconds=90), usegmt=True)
    assert 80.0 <= (retry_after_s(httpx.Response(503, headers={"Retry-After": later})) or 0) <= 90.0
    assert retry_after_s(httpx.Response(429, headers={"Retry-After": "morgen"})) is None
    assert retry_after_s(httpx.Response(429)) is None
    assert retry_after_s(httpx.Response(500, headers={"Retry-After": "5"})) is None  # alleen 429/503 (§5)
    assert retry_after_s(httpx.Response(429, headers={"Retry-After": "999999"})) == 3600.0
    assert MAX_INLINE_WAIT_S <= 8.0
