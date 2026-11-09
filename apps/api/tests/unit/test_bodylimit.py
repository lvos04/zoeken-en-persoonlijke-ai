"""Bodygrens (contract 1.2 §5) en netjes afsluiten na een 413 (ADR-0012, RFC 9112 §9.6).

Na het overschrijden leest de laag de rest van de body nog en gooit hem weg vóór de 413, zodat de verbinding zonder
TCP-reset sluit en de client de 413 echt ziet. Dat lezen is begrensd in bytes en tijd, en er komt nooit iets bij de
app.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from search_ai.bodylimit import BodyLimitMiddleware

Message = dict[str, Any]


class Recorder:
    def __init__(self) -> None:
        self.called = False

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.called = True


def _scope(content_length: int | None = None) -> dict[str, Any]:
    headers = [(b"content-type", b"application/json")]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    return {"type": "http", "method": "POST", "path": "/search/v1/sources", "headers": headers}


def _run(
    mw: BodyLimitMiddleware, scope: dict[str, Any], receive: Callable[[], Awaitable[Message]]
) -> tuple[list[Message], list[str]]:
    sent: list[Message] = []
    order: list[str] = []

    async def send(message: Message) -> None:
        order.append("send")
        sent.append(message)

    async def recv() -> Message:
        order.append("receive")
        return await receive()

    asyncio.run(mw(scope, recv, send))
    return sent, order


def _chunks(sizes: list[int], *, ends: bool = True) -> Callable[[], Awaitable[Message]]:
    queue = [
        {"type": "http.request", "body": b"x" * n, "more_body": not ends or i < len(sizes) - 1}
        for i, n in enumerate(sizes)
    ]
    reads = {"n": 0}

    async def receive() -> Message:
        reads["n"] += 1
        if queue:
            return queue.pop(0)
        await asyncio.sleep(3600)  # zou nooit mogen gebeuren: meer lezen dan er is
        return {"type": "http.disconnect"}

    receive.reads = reads  # type: ignore[attr-defined]
    return receive


def _status(sent: list[Message]) -> tuple[int, Any]:
    return int(sent[0]["status"]), json.loads(sent[1]["body"])


def test_chunked_over_the_limit_drains_the_rest_before_413() -> None:
    app = Recorder()
    receive = _chunks([65536] * 17 + [0])  # 17 × 64 KiB > 1 MiB, daarna het afsluitende lege stuk
    sent, order = _run(BodyLimitMiddleware(app), _scope(), receive)
    status, body = _status(sent)
    assert status == 413 and body["code"] == "payload_too_large" and body["details"] == {"max_bytes": 1048576}
    assert receive.reads["n"] == 18  # type: ignore[attr-defined]  # alles weggelezen …
    assert order.index("send") == len(order) - 2  # … en pas daarna het antwoord
    assert not app.called  # er komt nooit iets bij de app


def test_drain_is_bounded_in_bytes() -> None:
    app = Recorder()
    receive = _chunks([65536] * 200)  # 12,5 MiB, veel meer dan grens + draingrens
    mw = BodyLimitMiddleware(app, drain_max_bytes=256 * 1024)
    sent, _ = _run(mw, _scope(), receive)
    assert sent[0]["status"] == 413 and not app.called
    assert receive.reads["n"] <= 17 + 5  # type: ignore[attr-defined]  # hooguit ~256 KiB extra gelezen


def test_drain_is_bounded_in_time() -> None:
    app = Recorder()
    first = _chunks([65536] * 17, ends=False)

    async def slow() -> Message:
        if first.reads["n"] < 17:  # type: ignore[attr-defined]
            return await first()
        await asyncio.sleep(3600)  # een client die na de grens niets meer stuurt
        return {"type": "http.disconnect"}

    mw = BodyLimitMiddleware(app, drain_max_s=0.2)
    sent, _ = _run(mw, _scope(), slow)
    assert sent[0]["status"] == 413 and not app.called


def test_disconnect_while_draining_sends_nothing_more_than_the_413() -> None:
    app = Recorder()
    queue: list[Message] = [{"type": "http.request", "body": b"x" * 65536, "more_body": True} for _ in range(17)]
    queue.append({"type": "http.disconnect"})

    async def receive() -> Message:
        return queue.pop(0)

    sent, _ = _run(BodyLimitMiddleware(app), _scope(), receive)
    assert sent[0]["status"] == 413 and len(sent) == 2 and not app.called


def test_content_length_over_the_limit_is_refused_without_reaching_the_app() -> None:
    app = Recorder()
    receive = _chunks([1024 * 1024 + 1])
    sent, order = _run(BodyLimitMiddleware(app), _scope(1024 * 1024 + 1), receive)
    assert _status(sent)[0] == 413 and not app.called
    assert order[-2:] == ["send", "send"]  # de (kleine) rest is weggelezen, daarna de 413
    # een Content-Length ver boven grens + draingrens: meteen 413, er wordt niets gelezen
    app2, receive2 = Recorder(), _chunks([10])
    sent, order = _run(BodyLimitMiddleware(app2), _scope(50 * 1024 * 1024), receive2)
    assert _status(sent)[0] == 413 and "receive" not in order and not app2.called


def test_exactly_one_mib_reaches_the_app() -> None:
    got: dict[str, Any] = {}

    async def app(scope: Any, receive: Any, send: Any) -> None:
        msg = await receive()
        got["len"], got["more"] = len(msg["body"]), msg["more_body"]

    sent, _ = _run(BodyLimitMiddleware(app), _scope(), _chunks([524288, 524288]))
    assert sent == [] and got == {"len": 1048576, "more": False}
