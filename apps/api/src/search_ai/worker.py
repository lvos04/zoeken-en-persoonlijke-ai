"""Worker: koppelen met het omgevingstoken, outbox pushen, nacontrole van intrekkingen, retentie en opruimen.

Ingest (B07-002) draait in P0 bewust **niet**: er wordt niets gelezen of geïndexeerd.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field

import httpx
from sqlalchemy import delete

from . import events
from .config import Settings
from .db import Database
from .enrollment import EVENTS_INBOX_PATH, EnrollmentService
from .hubclient import HubClient
from .idempotency import purge_expired
from .search.models import SearchQuery
from .search.service import sweep_revoked
from .security.secrets import ensure_secrets, load_identity, load_vault_key, public_jwk
from .security.vault import Vault
from .timeutil import utcnow
from .voice.models import Transcript

log = logging.getLogger("search_ai.worker")


@dataclass
class WorkerState:
    next_enroll_at: float = 0.0
    enroll_failures: int = 0
    last_purge: float = 0.0
    stats: dict[str, int] = field(
        default_factory=lambda: {"pushed": 0, "push_failed": 0, "revoked_resweeps": 0, "expired_rows": 0}
    )


class Worker:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        ensure_secrets(settings.secrets_dir)
        self.vault = Vault(load_vault_key(settings.secrets_dir))
        self.db = Database(settings.database_url)
        self.hub = HubClient(settings, transport=transport)
        self.enrollment = EnrollmentService(
            settings, self.vault, public_jwk(load_identity(settings.secrets_dir)), self.hub
        )
        self.state = WorkerState()

    def enroll_step(self, now: float) -> None:
        if now < self.state.next_enroll_at:
            return
        with self.db.sessions() as s:
            outcome = self.enrollment.enroll_with_env_token(s)
            s.commit()
        if outcome is None or outcome.ok:
            self.state.enroll_failures = 0
            return
        if outcome.retryable:
            self.state.enroll_failures += 1
            delay = min(900.0, 15.0 * (2 ** (self.state.enroll_failures - 1))) * random.uniform(0.8, 1.2)  # noqa: S311
            self.state.next_enroll_at = now + delay

    def push_step(self) -> None:
        with self.db.sessions() as s:
            st = self.enrollment.state(s)
            if st.get("status") != "gekoppeld":
                return  # niet gekoppeld: events blijven 'pending' (geen poging geteld); de Hub kan ook pollen
            token = self.enrollment.service_token(s)
            hub_url = st.get("hub_url")
            if not token or not hub_url:
                return
            for ev in events.due_for_push(s):
                try:
                    resp = self.hub.request(
                        "POST",
                        hub_url,
                        EVENTS_INBOX_PATH,
                        json=events.envelope(ev),
                        headers={"Authorization": f"Bearer {token}"},
                        attempts=1,
                    )
                    ok = resp.status_code in (200, 201, 202, 204, 409)  # 409 = al ontvangen (dedupe in de Hub)
                    err = f"http_{resp.status_code}"
                except httpx.HTTPError as exc:
                    ok, err = False, type(exc).__name__
                if ok:
                    events.mark_delivered(ev)
                    self.state.stats["pushed"] += 1
                else:
                    events.mark_failed(ev, err)
                    self.state.stats["push_failed"] += 1
            s.commit()

    def housekeeping_step(self, now: float) -> None:
        with self.db.sessions() as s:
            self.state.stats["revoked_resweeps"] += sweep_revoked(s)
            n = 0
            for model in (SearchQuery, Transcript):
                res = s.execute(delete(model).where(model.expires_at <= utcnow()))
                n += int(getattr(res, "rowcount", 0) or 0)
            self.state.stats["expired_rows"] += n
            if now - self.state.last_purge > 3600:
                purge_expired(s)
                self.state.last_purge = now
            s.commit()

    def run_once(self) -> None:
        now = time.monotonic()
        for step in (lambda: self.enroll_step(now), self.push_step, lambda: self.housekeeping_step(now)):
            try:
                step()
            except Exception:  # een fout in één stap mag de andere niet blokkeren
                log.exception("workerstap mislukt")

    def run(self, stop: threading.Event) -> None:
        log.info(
            "worker gestart (interval %.0f s); ingest is in deze versie niet actief", self.settings.worker_interval_s
        )
        while not stop.is_set():
            self.run_once()
            stop.wait(self.settings.worker_interval_s)
        self.db.dispose()
        log.info("worker gestopt")
