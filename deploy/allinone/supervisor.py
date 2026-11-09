"""Zoeken en persoonlijke AI — procesbewaker van de alles-in-één image (PID 1 van de container).

Eén container draait alles: PostgreSQL 16 met pgvector (alleen unix-socket, geen netwerk, geen wachtwoord),
migraties, de API met statuspagina en de worker. Eén datamap (/appdata) en één poort (30450): zo installeert de app
als gewone TrueNAS Custom App zonder YAML. Shares worden als Host Path met 'Read Only' onder /bronnen gekoppeld.

Regels (gebaseerd op het werkende MSO-voorbeeld):
- Start als root alleen om /appdata (en alleen de eigen submappen) aan 568:568 te geven; daarna draait elk proces
  als 568 (PostgreSQL weigert root). **/bronnen wordt nooit aangeraakt** (geen chown/chmod: alleen lezen).
- Na de gebruikerswissel zet de bewaker zelf ``no_new_privs`` (prctl) voor zichzelf en elk kindproces (contract 1.2
  §2.2): het standaard Custom App-formulier van TrueNAS zet geen no-new-privileges en geen cap_drop (ADR-0010).
- De bewaker is zelf **PID 1** (geen root-init zoals dumb-init): na de eigenaarschapsstap draait ook PID 1 als 568, met
  ``CapEff 0`` en ``NoNewPrivs 1`` (contract 1.2 §2.2/§10, R11). Als PID 1 ruimt hij verweesde processen (zombies) op
  en vangt hij SIGTERM/SIGINT zelf af (PID 1 krijgt anders geen standaard-signaalafhandeling).
- First-start-init: ontbrekende geheimen in /appdata/secrets (0600, nooit overschrijven).
- Duidelijke Nederlandse fout (en even wachten) als /appdata ontbreekt of niet schrijfbaar is, i.p.v. een crashloop.
- Oude postmaster.pid na een harde stop wordt opgeruimd.
- Vóór openstaande migraties een pg_dump naar /appdata/backups (de nieuwste 3 blijven); mislukt die, dan start de
  app gewoon door. Mislukt een migratie, dan blijft de vorige database ongewijzigd en stopt de app met een melding.
- Crashende processen worden met backoff herstart; SIGTERM stopt worker → api → database netjes.
"""

from __future__ import annotations

import ctypes
import grp
import os
import pwd
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

UID = int(os.environ.get("SEARCH_AI_UID", "568"))
GID = int(os.environ.get("SEARCH_AI_GID", "568"))
DATA = Path(os.environ.get("SEARCH_AI_DATA_DIR", "/appdata"))
PGDATA = DATA / "postgres"
SOCKET_DIR = Path("/tmp/search-ai-pg")
PORT = os.environ.get("SEARCH_AI_PORT", "30450")
DB_USER = "search_ai"
DB_NAME = "search_ai"
DB_URL = f"postgresql+psycopg://{DB_USER}@/{DB_NAME}?host={SOCKET_DIR}"
MANAGED = ("postgres", "secrets", "backups", "logs")
STOP = threading.Event()
PR_SET_NO_NEW_PRIVS = 38


def log(msg: str) -> None:
    print(f"[zoeken-en-persoonlijke-ai] {msg}", flush=True)


def fail(msg: str, code: int = 78) -> None:
    log("FOUT: " + msg)
    time.sleep(30)  # houd de melding zichtbaar in de TrueNAS-app-log i.p.v. een snelle crashloop
    sys.exit(code)


def set_no_new_privs() -> bool:
    """Contract §10 'no-new-privileges' in de image zelf: daarna wint dit proces (en elk kind) geen rechten meer, ook
    niet via setuid-bestanden. Stil, zodat het ook als ``preexec_fn`` werkt; ``no_new_privs_active`` controleert."""
    try:
        return int(ctypes.CDLL(None, use_errno=True).prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)) == 0
    except (OSError, AttributeError):
        return False


def no_new_privs_active() -> bool:
    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            return any(line.split() == ["NoNewPrivs:", "1"] for line in fh)
    except OSError:
        return False


def demote() -> None:
    if os.getuid() == 0:
        os.setgroups([])
        os.setgid(GID)
        os.setuid(UID)  # van 0 naar 568: permitted/effective capabilities vervallen (geen ambient caps)
    set_no_new_privs()


def reap_orphans(known: set[int]) -> int:
    """PID 1: verweesde zombie-kinderen opruimen, zonder de exitstatus van eigen kinderen (``known``) weg te nemen.

    Leest ``/proc`` en wacht alleen op processen die zombie zijn, ons als ouder hebben en niet in ``known`` staan.
    """
    me, reaped = os.getpid(), 0
    try:
        entries = [e for e in os.listdir("/proc") if e.isdigit()]
    except OSError:
        return 0
    for entry in entries:
        pid = int(entry)
        if pid in known:
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="ascii", errors="replace") as fh:
                rest = fh.read().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if len(rest) >= 2 and rest[0] == "Z" and rest[1] == str(me):
            try:
                if os.waitpid(pid, os.WNOHANG)[0] == pid:
                    reaped += 1
            except ChildProcessError:
                pass
    return reaped


def _chown_tree(path: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(path):
        for name in [
            dirpath,
            *[os.path.join(dirpath, n) for n in dirnames + filenames],
        ]:
            try:
                os.lchown(name, UID, GID)
            except OSError:
                pass


def check_run_user() -> None:
    """Alleen root (die daarna naar 568 gaat) of 568 zelf kan de ingebouwde database starten.

    Elke andere gebruiker (bv. 'Custom User' 1000 in het TrueNAS-formulier) laat initdb anders vastlopen met een
    onduidelijke Engelse melding ('could not look up effective user ID'); geef in plaats daarvan een duidelijke fout.
    """
    uid, gid = os.getuid(), os.getgid()
    if uid != 0 and (uid, gid) != (UID, GID):
        fail(
            f"de app draait als gebruiker {uid}:{gid}, maar de ingebouwde database kan alleen als {UID}:{GID} (apps) "
            f"draaien. Zet in de app-instellingen 'Custom User' uit (dan regelt de app de rechten zelf) of gebruik "
            f"gebruiker en groep {UID}."
        )


def prepare_dirs() -> None:
    if not DATA.exists():
        fail(f"{DATA} bestaat niet. Koppel in de app-instellingen een dataset (Host Path) aan {DATA}.")
    root = os.getuid() == 0
    if root:
        st = DATA.stat()
        if (st.st_uid, st.st_gid) != (UID, GID):
            log(f"eigenaar van {DATA} wordt {UID}:{GID}")
            os.chown(DATA, UID, GID)
        for sub in MANAGED:
            p = DATA / sub
            if p.exists() and (p.stat().st_uid, p.stat().st_gid) != (UID, GID):
                log(f"eigenaar van {p} wordt {UID}:{GID}")
                _chown_tree(p)
    for sub in (PGDATA, DATA / "secrets", DATA / "backups"):
        try:
            sub.mkdir(parents=True, exist_ok=True)
            if root:
                os.chown(sub, UID, GID)
        except PermissionError:
            fail(
                f"geen schrijfrechten op {sub}. Laat de app als root starten (dan regelt hij de rechten zelf) of "
                f"geef gebruiker {UID} (apps) schrijfrechten op de dataset."
            )
    try:
        os.chmod(PGDATA, 0o700)
        os.chmod(DATA / "secrets", 0o700)
    except PermissionError:
        fail(f"{PGDATA} is niet van gebruiker {UID}; start de app als root of maak {UID} eigenaar van de dataset.")
    SOCKET_DIR.mkdir(parents=True, exist_ok=True)
    if root:
        os.chown(SOCKET_DIR, UID, GID)
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            f"open('{DATA}/.search-ai-schrijftest','w').close();import os;os.remove('{DATA}/.search-ai-schrijftest')",
        ],
        preexec_fn=demote,
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        fail(f"gebruiker {UID} kan niet schrijven in {DATA}. Pas de rechten van de dataset aan (gebruiker apps).")
    report_sources()


def report_sources() -> None:
    """Meldt (alleen lezen) welke shares onder /bronnen hangen en of ze alleen-lezen zijn. Wijzigt niets."""
    base = Path(os.environ.get("SEARCH_AI_READ_ROOTS", "/bronnen").split(",")[0].strip() or "/bronnen")
    try:
        entries = sorted(p for p in base.iterdir() if p.is_dir())
    except OSError:
        entries = []
    if not entries:
        log(f"geen shares gekoppeld onder {base} (optioneel; zonder shares wordt niets geïndexeerd)")
        return
    for p in entries:
        try:
            ro = bool(os.statvfs(p).f_flag & os.ST_RDONLY)
        except OSError:
            ro = False
        log(f"share {p}: {'alleen-lezen' if ro else 'LET OP: schrijfbaar — zet Read Only aan in de app-instellingen'}")


def env_for(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "HOME": "/tmp",
            "SEARCH_AI_DATA_DIR": str(DATA),
            "SEARCH_AI_DATABASE_URL": DB_URL,
            "PYTHONUNBUFFERED": "1",
        }
    )
    try:
        env["USER"] = pwd.getpwuid(UID).pw_name
    except KeyError:
        pass
    env.update(extra or {})
    return env


def run_once(name: str, cmd: list[str]) -> int:
    log(f"{name} …")
    return subprocess.run(cmd, env=env_for(), preexec_fn=demote, check=False).returncode


class Child:
    def __init__(self, name: str, cmd: list[str], stop_signal: int = signal.SIGTERM) -> None:
        self.name, self.cmd, self.stop_signal = name, cmd, stop_signal
        self.proc: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        log(f"start {self.name}")
        self.proc = subprocess.Popen(self.cmd, env=env_for(), preexec_fn=demote)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout: float = 25) -> None:
        if not self.alive() or self.proc is None:
            return
        self.proc.send_signal(self.stop_signal)
        try:
            self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            log(f"{self.name} reageert niet; geforceerd stoppen")
            self.proc.kill()
            self.proc.wait(5)


def init_database() -> None:
    if (PGDATA / "PG_VERSION").exists():
        major = (PGDATA / "PG_VERSION").read_text().strip()
        if major != "16":
            fail(f"de database in {PGDATA} is PostgreSQL {major}; deze image gebruikt 16. Zie de releasenotes.")
        pid = PGDATA / "postmaster.pid"
        if pid.exists():
            log("oude postmaster.pid van een onderbroken stop verwijderd")
            pid.unlink()
        return
    log("eerste start: database aanmaken")
    rc = run_once(
        "initdb",
        [
            "initdb",
            "-D",
            str(PGDATA),
            "-U",
            DB_USER,
            "--auth-local=trust",
            "--auth-host=reject",
            "--encoding=UTF8",
            "--locale=C.UTF-8",
            "--no-instructions",
        ],
    )
    if rc != 0:
        fail("initdb mislukt (zie de log hierboven)")


def postgres_child() -> Child:
    return Child(
        "database",
        [
            "postgres",
            "-D",
            str(PGDATA),
            "-c",
            "listen_addresses=",
            "-c",
            f"unix_socket_directories={SOCKET_DIR}",
            "-c",
            "unix_socket_permissions=0700",  # alleen gebruiker 568 (de app zelf) kan de database bereiken
            "-c",
            "max_connections=30",
            "-c",
            "shared_buffers=64MB",
            "-c",
            "maintenance_work_mem=64MB",
            "-c",
            "jit=off",
            "-c",
            "logging_collector=off",
            "-c",
            "log_min_messages=warning",
        ],
        stop_signal=signal.SIGINT,  # 'fast shutdown'
    )


def wait_for_db(pg: Child, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pg.alive():
            fail("de database stopte tijdens het opstarten (zie de log hierboven)")
        rc = subprocess.run(
            [
                "pg_isready",
                "-h",
                str(SOCKET_DIR),
                "-U",
                DB_USER,
                "-d",
                "postgres",
                "-q",
            ],
            env=env_for(),
            preexec_fn=demote,
            check=False,
        ).returncode
        if rc == 0:
            break
        time.sleep(1)
    else:
        fail("de database was niet binnen 90 s klaar")
    exists = subprocess.run(
        [
            "psql",
            "-h",
            str(SOCKET_DIR),
            "-U",
            DB_USER,
            "-d",
            "postgres",
            "-tAc",
            f"SELECT 1 FROM pg_database WHERE datname='{DB_NAME}'",  # noqa: S608 - vaste constante, geen invoer
        ],
        env=env_for(),
        preexec_fn=demote,
        capture_output=True,
        text=True,
        check=False,
    )
    if (
        exists.stdout.strip() != "1"
        and run_once(
            f"database '{DB_NAME}' aanmaken",
            [
                "psql",
                "-h",
                str(SOCKET_DIR),
                "-U",
                DB_USER,
                "-d",
                "postgres",
                "-qc",
                f"CREATE DATABASE {DB_NAME}",
            ],
        )
        != 0
    ):
        fail(f"database '{DB_NAME}' aanmaken mislukt")


def shutdown(signum: int, _frame: object) -> None:
    log(f"signaal {signum}: netjes stoppen")
    STOP.set()


def main() -> None:
    # als PID 1 eerst zelf de stopsignalen afvangen (anders negeert de kernel SIGTERM van 'docker stop')
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    version = os.environ.get("SEARCH_AI_VERSION", "?")
    log(f"Zoeken en persoonlijke AI (alles-in-één) · versie {version} · data {DATA} · poort {PORT} · pid {os.getpid()}")
    check_run_user()
    prepare_dirs()
    demote()  # vanaf hier draait niets meer als root, en met no_new_privs (erft over op alle kinderen)
    if not no_new_privs_active():
        log("WAARSCHUWING: no-new-privileges kon niet worden gezet (prctl); de processen draaien wel als 568")
    py = sys.executable
    if run_once("geheimen controleren (first-start-init)", [py, "-m", "search_ai", "init"]) != 0:
        fail("geheimen aanmaken in /appdata/secrets mislukt")
    init_database()
    pg = postgres_child()
    pg.start()
    wait_for_db(pg)
    if (
        run_once(
            "migraties (met back-up vóór upgrade)",
            [py, "-m", "search_ai", "migrate", "--backup-dir", str(DATA / "backups")],
        )
        != 0
    ):
        pg.stop()
        fail("databasemigratie mislukt; de vorige database is ongewijzigd. Zet de vorige image-tag terug.")

    children = [
        Child("api", [py, "-m", "search_ai", "api", "--host", "0.0.0.0", "--port", PORT]),  # noqa: S104 - container
        Child("worker", [py, "-m", "search_ai", "worker"]),
    ]
    for c in children:
        if not STOP.is_set():
            c.start()

    backoff: dict[str, float] = {}
    while not STOP.is_set():
        reap_orphans({c.proc.pid for c in [pg, *children] if c.proc is not None})
        if not pg.alive():
            log("database gestopt; opnieuw starten")
            for c in children:
                c.stop()
            init_database()
            pg.start()
            wait_for_db(pg)
            for c in children:
                c.start()
        for c in children:
            if not c.alive() and not STOP.is_set():
                wait = min(60.0, backoff.get(c.name, 1.0))
                log(f"{c.name} gestopt (code {c.proc.returncode if c.proc else '?'}); herstart over {wait:.0f} s")
                STOP.wait(wait)
                backoff[c.name] = wait * 2
                if not STOP.is_set():
                    c.start()
            elif c.alive():
                backoff[c.name] = max(1.0, backoff.get(c.name, 1.0) * 0.9)
        STOP.wait(2)

    for c in reversed(children):  # worker → api
        c.stop()
    pg.stop(60)
    log("gestopt")


if __name__ == "__main__":
    try:
        pwd.getpwuid(UID)
        grp.getgrgid(GID)
    except KeyError:
        fail(f"gebruiker {UID}:{GID} bestaat niet in de image")
    if shutil.which("postgres") is None:
        fail("postgres ontbreekt in de image")
    main()
