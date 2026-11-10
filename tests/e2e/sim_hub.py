"""Hub-simulator als eigen proces voor de dienst-E2E. ALLEEN voor tests; geen productiecode.

Dunne HTTP-schil om ``apps/api/tests/hubsim.py`` (dezelfde kern als in de integratietests), die platformcontract 1.3
volgt: enroll exact volgens §3.3 (422/401/429/herhaling), ``services/self``, ``self/rotate``, ``events/inbox``,
``key-rotations`` en ``jwks``. Testhulpmiddelen onder ``/sim/*`` (koppeltoken en koppelcode maken, JWT's uitgeven, intrekken,
rotatie aanvragen, inbox en alle verzoeken lezen).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "api"))

from tests.hubsim import HubSim

SIM = HubSim()
app = FastAPI(title="sim-hub")


@app.get("/sim/fingerprint")
def sim_fingerprint() -> dict[str, str]:
    return {"fingerprint": SIM.fingerprint()}


@app.post("/sim/enrollment-token")
def sim_enrollment_token() -> dict[str, str]:
    return {"token": SIM.add_enrollment_token()}


@app.post("/sim/pairing-code")
def sim_pairing_code() -> dict[str, str]:
    """Koppelcode zoals de Hub-UI die maakt (voor de ontwerpreview met de echte statuspagina)."""
    return {"code": SIM.add_pairing_code()}


@app.post("/sim/token")
async def sim_token(request: Request) -> dict[str, str]:
    body = await request.json()
    return {"token": SIM.user_jwt(list(body.get("roles", [])), sub=str(body.get("sub", "e2e-user")))}


@app.post("/sim/service-jwt")
def sim_service_jwt() -> dict[str, str]:
    return {"token": SIM.service_jwt()}


@app.post("/sim/revoke")
def sim_revoke() -> dict[str, str]:
    SIM.revoke()
    return {"status": "ingetrokken"}


@app.post("/sim/request-rotation")
def sim_request_rotation() -> dict[str, str]:
    SIM.request_rotation()
    return {"status": "aangevraagd"}


@app.get("/sim/service-token")
def sim_service_token() -> dict[str, Any]:
    return {"token": SIM.current_token(), "issued": len(SIM.issued)}


@app.get("/sim/inbox")
def sim_inbox() -> list[dict[str, Any]]:
    return list(SIM.inbox)


@app.get("/sim/requests")
def sim_requests() -> list[dict[str, Any]]:
    return list(SIM.requests)


@app.get("/sim/enroll-requests")
def sim_enroll_requests() -> list[dict[str, Any]]:
    return list(SIM.enroll_requests)


@app.api_route("/api/{rest:path}", methods=["GET", "POST"])
async def hub_api(request: Request, rest: str) -> Response:
    status, headers, body = SIM.handle(
        request.method,
        f"/api/{rest}",
        request.url.query,
        {k.lower(): v for k, v in request.headers.items()},
        await request.body(),
        request.client.host if request.client else "",
    )
    return JSONResponse(body, status_code=status, headers=headers)
