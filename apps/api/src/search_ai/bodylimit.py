"""Verzoekgrootte (contract 1.2 §5): ASGI-laag **vóór routering en authenticatie**.

- Een te grote ``Content-Length`` wordt direct geweigerd.
- Zonder ``Content-Length`` (bv. ``Transfer-Encoding: chunked``) wordt de body tijdens het lezen geteld; bij meer dan
  1 MiB (1 048 576 bytes) volgt meteen ``413`` en wordt de rest niet gelezen.
- Antwoord: ``413 {"code": "payload_too_large", "details": {"max_bytes": 1048576}}`` in het contractformaat.

De body wordt (tot de grens) gebufferd en daarna ongewijzigd aan de app doorgegeven. Deze dienst heeft geen uploads;
een hogere grens per route zou een ADR en een eigen bovengrens vragen.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_BODY_BYTES = 1024 * 1024
TRACE_HEADER = b"x-trace-id"


def _trace_id(scope: Scope) -> str:
    for name, value in scope.get("headers") or []:
        if name.lower() == TRACE_HEADER:
            try:
                return str(uuid.UUID(value.decode("latin-1")))
            except ValueError:
                break
    return str(uuid.uuid4())


async def _send_error(send: Send, scope: Scope, status: int, code: str, message: str, details: dict[str, Any]) -> None:
    tid = _trace_id(scope)
    body = json.dumps(
        {"code": code, "message": message, "details": details, "trace_id": tid}, ensure_ascii=False
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"x-trace-id", tid.encode("ascii")),
                (b"x-content-type-options", b"nosniff"),
                (b"cache-control", b"no-store"),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def _too_large(self, send: Send, scope: Scope) -> None:
        await _send_error(
            send,
            scope,
            413,
            "payload_too_large",
            "Het verzoek is te groot (maximaal 1 MiB).",
            {"max_bytes": self.max_bytes},
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers") or []:
            if name.lower() == b"content-length":
                raw = value.decode("latin-1").strip()
                if not raw.isdigit():
                    await _send_error(send, scope, 400, "bad_request", "Ongeldige Content-Length.", {})
                    return
                if int(raw) > self.max_bytes:
                    await self._too_large(send, scope)
                    return
        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return  # de client is weg: niets te doen
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > self.max_bytes:
                await self._too_large(send, scope)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()  # na de body: wacht op http.disconnect zoals gewoonlijk

        await self.app(scope, replay, send)
