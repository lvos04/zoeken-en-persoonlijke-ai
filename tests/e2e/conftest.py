"""Dienst-E2E: echte processen (API + worker van zoeken-en-persoonlijke-ai) tegen een Hub-simulator in een eigen
proces, met nep-shares op schijf.

Vereist: de Python-omgeving met ``apps/api[dev]`` en een PostgreSQL met pgvector (standaard de lokale ontwikkel-
PostgreSQL; database ``zoek_test_e2e``). Geen echte NAS, Hub, Immich, Jellyfin of accounts.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, text

ADMIN_DB_URL = os.environ.get("SEARCH_AI_TEST_ADMIN_URL", "postgresql+psycopg://postgres@127.0.0.1:5432/postgres")
E2E_DB = "zoek_test_e2e"
HERE = Path(__file__).resolve().parent


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return int(port)


def wait_http(url: str, timeout: float = 40) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"{url} kwam niet op")


class FreshAuth(Mapping[str, str]):
    """Bij elke lezing een nieuwe Hub-JWT (nieuwe ``jti``), zoals de Hub per mutatie doet (contract 1.3 §4.6)."""

    def __init__(self, mint: Callable[[], str]) -> None:
        self._mint = mint

    def __getitem__(self, key: str) -> str:
        if key != "Authorization":
            raise KeyError(key)
        return f"Bearer {self._mint()}"

    def __iter__(self) -> Iterator[str]:
        return iter(("Authorization",))

    def __len__(self) -> int:
        return 1


@dataclass
class Stack:
    api: str
    hub: str
    db_url: str
    shares: Path
    data: Path
    fingerprint: str
    hub_proc: subprocess.Popen[bytes]
    procs: list[subprocess.Popen[bytes]] = field(default_factory=list)

    def token(self, *roles: str, sub: str = "e2e-user") -> Mapping[str, str]:
        def mint() -> str:
            r = httpx.post(f"{self.hub}/sim/token", json={"roles": list(roles), "sub": sub}, timeout=5)
            return str(r.json()["token"])

        return FreshAuth(mint)

    def service(self) -> Mapping[str, str]:
        return FreshAuth(lambda: str(httpx.post(f"{self.hub}/sim/service-jwt", timeout=5).json()["token"]))

    def idem(self, headers: Mapping[str, str], key: str | None = None) -> dict[str, str]:
        return {**headers, "Idempotency-Key": key or str(uuid.uuid4())}


def _make_shares(root: Path) -> Path:
    a = root / "documenten"
    b = root / "prive-b"
    (a / "werk").mkdir(parents=True)
    b.mkdir(parents=True)
    (a / "werk" / "offerte.txt").write_text("Offerte voor de keuken.", encoding="utf-8")
    (a / "notities.md").write_text("Boodschappen.", encoding="utf-8")
    (b / "dagboek.txt").write_text("Privé dagboek van B.", encoding="utf-8")
    os.symlink(b / "dagboek.txt", a / "link-naar-b.txt")
    return root


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    try:
        eng = create_engine(ADMIN_DB_URL, isolation_level="AUTOCOMMIT")
        with eng.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{E2E_DB}" WITH (FORCE)'))
            c.execute(text(f'CREATE DATABASE "{E2E_DB}"'))
        eng.dispose()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"PostgreSQL niet beschikbaar: {type(exc).__name__}")
    db_url = ADMIN_DB_URL.rsplit("/", 1)[0] + "/" + E2E_DB
    hub_port, api_port = free_port(), free_port()
    hub_url, api_url = f"http://127.0.0.1:{hub_port}", f"http://127.0.0.1:{api_port}"
    data = tmp_path_factory.mktemp("appdata")
    shares = _make_shares(tmp_path_factory.mktemp("bronnen"))
    logs = tmp_path_factory.mktemp("logs")
    base_env = {k: v for k, v in os.environ.items() if not k.startswith(("SEARCH_AI_", "HUB_"))}
    for k in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        base_env.pop(k, None)  # alles blijft lokaal
    hub_env = {**base_env, "PYTHONPATH": str(HERE)}
    hub_proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "sim_hub:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(hub_port),
        ],
        env=hub_env,
        cwd=str(HERE),
        stdout=open(logs / "hub.log", "wb"),
        stderr=subprocess.STDOUT,
    )
    wait_http(f"{hub_url}/sim/fingerprint")  # niet via /api: dat zou in de verzoeklijst van de Hub belanden
    # zoals de Hub bij installatie via de API: eenmalig token + verwachte vingerafdruk (contract 1.3 §3.2)
    enroll_token = httpx.post(f"{hub_url}/sim/enrollment-token", timeout=5).json()["token"]
    fingerprint = httpx.get(f"{hub_url}/sim/fingerprint", timeout=5).json()["fingerprint"]
    svc_env = {
        **base_env,
        "SEARCH_AI_DATA_DIR": str(data),
        "SEARCH_AI_DATABASE_URL": db_url,
        "SEARCH_AI_PORT": str(api_port),
        "SEARCH_AI_READ_ROOTS": str(shares),
        # tijdelijke testmappen zijn schrijfbaar; de alleen-lezen-eis toetsen de rooktest en de integratietests
        "SEARCH_AI_REQUIRE_READONLY_MOUNTS": "false",
        "SEARCH_AI_ALLOW_LOOPBACK_HUB": "1",
        "SEARCH_AI_WORKER_INTERVAL_S": "1",
        "SEARCH_AI_HUB_SELF_CHECK_S": "2",
        "HUB_URL": hub_url,
        "HUB_ENROLLMENT_TOKEN": enroll_token,
        "HUB_KEY_FINGERPRINT": fingerprint,
    }
    subprocess.run(
        [sys.executable, "-m", "search_ai", "migrate"],
        env=svc_env,
        check=True,
        capture_output=True,
    )
    st = Stack(
        api=api_url, hub=hub_url, db_url=db_url, shares=shares, data=data, fingerprint=fingerprint, hub_proc=hub_proc
    )
    for name, cmd in (
        (
            "api",
            [
                sys.executable,
                "-m",
                "search_ai",
                "api",
                "--host",
                "127.0.0.1",
                "--port",
                str(api_port),
            ],
        ),
        ("worker", [sys.executable, "-m", "search_ai", "worker"]),
    ):
        st.procs.append(
            subprocess.Popen(
                cmd,
                env=svc_env,
                stdout=open(logs / f"{name}.log", "wb"),
                stderr=subprocess.STDOUT,
            )
        )
    wait_http(f"{api_url}/healthz")
    try:
        yield st
    finally:
        for p in [*st.procs, hub_proc]:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        for p in [*st.procs, hub_proc]:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
