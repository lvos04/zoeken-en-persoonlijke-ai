"""Ontwerpreview van de statuspagina tegen de **echte dienst** (ALLEEN ontwikkeling; geen installatiemethode).

Start lokaal echte processen van zoeken-en-persoonlijke-ai (``python -m search_ai api``/``worker``, eigen PostgreSQL-
databases met pgvector) naast twee Hub-simulators (``tests/e2e/sim_hub.py``) en laat daarna ``echte_dienst.mjs`` in
Chromium alle toestanden van ``GET /`` doorlopen: koppelen via het formulier (fout en goed), gekoppeld met testbronnen,
ontkoppeld en ingetrokken door de Hub, niet gereed, te veel pogingen, verlopen formulier, onleesbare koppelstatus en
een onbruikbaar sjabloon.

    PATH=/usr/lib/postgresql/16/bin:$PATH .venv/bin/python tests/ui/echte_dienst.py \\
        --out build/echte-dienst [--rapport <json>] [--schermafdrukken <map>]

Vereist: een PostgreSQL met pgvector (standaard 127.0.0.1:5432, gebruiker postgres; databases ``zoek_ui_echt_*``) en
``npm ci`` in ``tests/ui``. Alle gegevens zijn fictief (simulator): geen echte NAS, Hub of account.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from sqlalchemy import create_engine, text

from search_ai.search.service import CONSENT_TEXT_VERSION

ROOT = Path(__file__).resolve().parents[2]
E2E = ROOT / "tests" / "e2e"
ADMIN_DB_URL = os.environ.get("SEARCH_AI_TEST_ADMIN_URL", "postgresql+psycopg://postgres@127.0.0.1:5432/postgres")
DATABASES = ("zoek_ui_echt_a", "zoek_ui_echt_b", "zoek_ui_echt_d")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def wait_http(url: str, timeout: float = 40) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            httpx.get(url, timeout=2)
            return
        except httpx.HTTPError:
            time.sleep(0.3)
    raise RuntimeError(f"{url} kwam niet op")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--rapport", type=Path)
    ap.add_argument("--schermafdrukken", type=Path)
    args = ap.parse_args()
    out: Path = args.out.resolve()
    if out.exists():
        shutil.rmtree(out)
    logs, run = out / "logs", out / "run"
    logs.mkdir(parents=True)
    run.mkdir()

    eng = create_engine(ADMIN_DB_URL, isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        for db in (*DATABASES, "zoek_ui_echt_ontbreekt"):
            c.execute(text(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)'))
        for db in DATABASES:
            c.execute(text(f'CREATE DATABASE "{db}"'))
    eng.dispose()
    base_db = ADMIN_DB_URL.rsplit("/", 1)[0]

    # nep-shares: twee mappen met een paar bestanden (alleen namen; er wordt niets geïndexeerd)
    shares = run / "bronnen"
    for sub, name, body in (("documenten", "notities.md", "Boodschappen."), ("fotos-2026", "lijst.txt", "Vakantie.")):
        (shares / sub).mkdir(parents=True)
        (shares / sub / name).write_text(body, encoding="utf-8")

    # een opzettelijk gewijzigde sjabloonkopie (één byte anders): de dienst moet die weigeren (peil_sjabloon_ongeldig)
    kapot = run / "peil-kapot"
    shutil.copytree(ROOT / "design" / "ontwerptaal", kapot)
    tpl = kapot / "statuspagina.html"
    tpl.write_text(tpl.read_text(encoding="utf-8").replace("<h1>", "<h1 >", 1), encoding="utf-8")

    env0 = {k: v for k, v in os.environ.items() if not k.startswith(("SEARCH_AI_", "HUB_"))}
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        env0.pop(k, None)
    env0["TZ"] = "Europe/Amsterdam"  # zoals TrueNAS de container-tijdzone zet; de pagina toont lokale tijd
    procs: list[subprocess.Popen[bytes]] = []

    def start(name: str, cmd: list[str], env: dict[str, str], cwd: Path = ROOT) -> None:
        log = open(logs / f"{name}.log", "wb")  # noqa: SIM115 - blijft open zolang het proces loopt
        procs.append(subprocess.Popen(cmd, env=env, cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT))  # noqa: S603

    sims: dict[str, str] = {}
    for name in ("a", "b"):
        port = free_port()
        start(
            f"sim-{name}",
            [sys.executable, "-m", "uvicorn", "sim_hub:app", "--host", "127.0.0.1", "--port", str(port)],
            {**env0, "PYTHONPATH": str(E2E)},
            cwd=E2E,
        )
        sims[name] = f"http://127.0.0.1:{port}"
    for url in sims.values():
        wait_http(f"{url}/sim/fingerprint")

    services: dict[str, str] = {}

    def service(name: str, db: str, *, worker: bool, extra: dict[str, str] | None = None, migrate: bool = True) -> None:
        port = free_port()
        data = run / f"appdata-{name}"
        data.mkdir()
        env = {
            **env0,
            "SEARCH_AI_DATA_DIR": str(data),
            "SEARCH_AI_DATABASE_URL": f"{base_db}/{db}",
            "SEARCH_AI_READ_ROOTS": f"{shares}/documenten,{shares}/fotos-2026",
            "SEARCH_AI_REQUIRE_READONLY_MOUNTS": "false",  # tijdelijke testmappen zijn schrijfbaar
            "SEARCH_AI_ALLOW_LOOPBACK_HUB": "1",  # de simulators draaien op 127.0.0.1
            "SEARCH_AI_WORKER_INTERVAL_S": "1",
            "SEARCH_AI_HUB_SELF_CHECK_S": "2",
            **(extra or {}),
        }
        if migrate:
            subprocess.run([sys.executable, "-m", "search_ai", "migrate"], env=env, check=True, capture_output=True)
        if name == "d":  # onleesbaar koppelbestand (bv. half geschreven of met de hand gewijzigd)
            (data / "secrets").mkdir(parents=True, exist_ok=True)
            (data / "secrets" / "hub-link.json").write_text('{"state": "gekoppeld", ', encoding="utf-8")
        start(
            f"api-{name}", [sys.executable, "-m", "search_ai", "api", "--host", "127.0.0.1", "--port", str(port)], env
        )
        if worker:
            start(f"worker-{name}", [sys.executable, "-m", "search_ai", "worker"], env)
        services[name] = f"http://127.0.0.1:{port}"

    fp_b = httpx.get(f"{sims['b']}/sim/fingerprint", timeout=5).json()["fingerprint"]
    token_b = httpx.post(f"{sims['b']}/sim/enrollment-token", timeout=5).json()["token"]
    service("a", "zoek_ui_echt_a", worker=True)  # nooit gekoppeld → koppelen via het formulier
    service(
        "b",
        "zoek_ui_echt_b",
        worker=True,  # koppelt zelf met het eenmalige token uit de omgeving (zoals bij installatie via de Hub)
        extra={"HUB_URL": sims["b"], "HUB_ENROLLMENT_TOKEN": token_b, "HUB_KEY_FINGERPRINT": fp_b},
    )
    service("c", "zoek_ui_echt_ontbreekt", worker=False, migrate=False)  # database bestaat niet → niet gereed
    service("d", "zoek_ui_echt_d", worker=False)
    service("e", "zoek_ui_echt_ontbreekt", worker=False, migrate=False, extra={"SEARCH_AI_PEIL_DIR": str(kapot)})
    for url in services.values():
        wait_http(f"{url}/healthz")

    config = {
        "sims": sims,
        "diensten": services,
        "fingerprint_a": httpx.get(f"{sims['a']}/sim/fingerprint", timeout=5).json()["fingerprint"],
        "bronnen": str(shares),
        "toestemming_versie": CONSENT_TEXT_VERSION,
    }
    (out / "config.json").write_text(json.dumps(config, indent=1) + "\n", encoding="utf-8")
    cmd = ["node", str(ROOT / "tests" / "ui" / "echte_dienst.mjs"), "--config", str(out / "config.json")]
    if args.rapport:
        cmd += ["--rapport", str(args.rapport.resolve())]
    if args.schermafdrukken:
        cmd += ["--schermafdrukken", str(args.schermafdrukken.resolve())]
    cmd += ["--alle-schermafdrukken", str(out / "schermafdrukken")]
    try:
        return subprocess.run(cmd, check=False).returncode  # noqa: S603
    finally:
        for p in procs:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        for p in procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
        eng = create_engine(ADMIN_DB_URL, isolation_level="AUTOCOMMIT")
        with eng.connect() as c:
            for db in DATABASES:
                c.execute(text(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)'))
        eng.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
