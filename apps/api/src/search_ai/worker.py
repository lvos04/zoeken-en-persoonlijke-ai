"""Worker: koppelen met het omgevingstoken, koppeling onderhouden, outbox pushen, retentie en opruimen.

Koppeling (contract 1.3 §3.3–§3.8, §4.5):
- env-token: backoff vanaf 5 s (×2, max. 15 min, met jitter), ``429`` → niet vóór ``Retry-After``; definitief na
  401/404/413/422, een ongeldig antwoord, een verkeerde vingerafdruk of 60 min na de eerste poging.
- ``gekoppeld`` zonder ``confirmed_at`` → alleen bevestigen met ``GET /api/v1/services/self``.
- Elke ~4 min ``services/self`` (zo wordt een rotatieverzoek binnen 5 min opgemerkt en een intrekking ontdekt).
- ``Hub-Token-Rotation: required`` → ``POST /api/v1/services/self/rotate`` (de aanvraag overleeft een herstart via
  ``token_rotation_requested_at`` in het koppelbestand).
- Vaste ``service.*``-events (§7, 1.3): ``service.hub_link.linked``/``unlinked``/``revoked``, ``service.token.rotated``,
  ``service.key_rotation.applied``/``rejected``, steeds met ``entity: {type: "service", id: <app_id>}``.
- Sleutelrotaties pollen hooguit elke 15 min.
Ingest (B07-002) draait in P0 bewust **niet**: er wordt niets gelezen of geïndexeerd.
"""

from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert

from . import APP_ID, audit, events
from .config import Settings
from .db import Database
from .enrollment import EVENTS_INBOX_PATH, EnrollmentService, Outcome, migrate_legacy_link
from .health import WORKER_HEARTBEAT_KEY
from .hubclient import HubClient
from .hublink import HubLinkStore
from .idempotency import purge_expired
from .models import JwtReplay, ServiceState
from .search.models import SearchQuery
from .search.service import expire_consents, sweep_revoked
from .security.secrets import ensure_secrets, load_identity, load_vault_key, public_jwk
from .security.vault import Vault
from .timeutil import iso, utcnow
from .voice.models import Transcript

log = logging.getLogger("search_ai.worker")


@dataclass
class WorkerState:
    started: bool = False
    next_enroll_at: float = 0.0
    enroll_failures: int = 0
    next_confirm_at: float = 0.0
    next_self_check_at: float = 0.0
    next_rotation_at: float = 0.0
    next_key_poll_at: float = 0.0
    last_purge: float = 0.0
    config_error_logged: str | None = None
    revocation_recorded: bool = False
    stats: dict[str, int] = field(
        default_factory=lambda: {
            "pushed": 0,
            "push_failed": 0,
            "expired_consents": 0,
            "revoked_resweeps": 0,
            "expired_rows": 0,
            "expired_replay": 0,
        }
    )


class Worker:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        ensure_secrets(settings.secrets_dir)
        self.vault = Vault(load_vault_key(settings.secrets_dir))
        self.db = Database(settings.database_url)
        self.hub = HubClient(settings, transport=transport)
        self.store = HubLinkStore(settings.secrets_dir, APP_ID)
        self.enrollment = EnrollmentService(
            settings, self.store, public_jwk(load_identity(settings.secrets_dir)), self.hub
        )
        self.state = WorkerState()

    # ------------------------------------------------------------------ hulp
    def _record(
        self,
        action: str,
        message_nl: str,
        event_type: str | None = None,
        payload: dict[str, Any] | None = None,
        **details: Any,
    ) -> None:
        """Audit + (optioneel) een vast ``service.*``-event (entiteit ``service``/<app_id>, §7). Faalt nooit hard (de
        koppeling staat al vast)."""
        tid = str(uuid.uuid4())
        try:
            with self.db.sessions() as s:
                audit.record(
                    s,
                    domain="platform",
                    action=action,
                    entity_type="hub_link",
                    entity_id="hub",
                    principal=None,
                    trace_id=tid,
                    message_nl=message_nl,
                    details=details,
                )
                if event_type:
                    events.emit_service(
                        s, event_type, actor_type="system", actor_id="worker", trace_id=tid, payload=payload or {}
                    )
                s.commit()
        except Exception:
            log.warning("auditspoor voor %s kon niet worden vastgelegd", action)

    def _linked(self) -> bool:
        return self.store.read_safe().get("state") == "gekoppeld"

    # ------------------------------------------------------------------ stappen
    def startup(self) -> None:
        if self.state.started:
            return
        self.state.started = True
        try:
            migrate_legacy_link(self.store, self.db, self.vault, self.settings.allow_loopback_hub)
        except Exception:
            log.warning("overzetten van de oude koppeling is uitgesteld (database niet beschikbaar)")
            self.state.started = False  # volgende ronde opnieuw
            return
        self.enrollment.on_worker_start()

    def enroll_step(self, now: float) -> None:
        if now < self.state.next_enroll_at:
            return
        outcome = self.enrollment.enroll_with_env_token()
        if outcome is None:
            self.state.enroll_failures = 0
            return
        if outcome.ok:
            self.state.enroll_failures = 0
            self.state.next_self_check_at = now + self.settings.hub_self_check_s
            self._record(
                "hub.pair",
                "Gekoppeld met de Hub via het koppeltoken uit de app-instellingen.",
                "service.hub_link.linked",
                {"via": "token"},
                via="token",
                herhaling=bool(outcome.detail.get("replayed")),
            )
            return
        if outcome.code in ("hub_adres_ongeldig", "vingerafdruk_ongeldig"):
            if self.state.config_error_logged != outcome.code:
                log.error("koppelen niet mogelijk: %s (pas de app-instellingen aan)", outcome.code)
                self.state.config_error_logged = outcome.code
            self.state.next_enroll_at = now + self.settings.enroll_backoff_max_s
            return
        if outcome.retryable:
            self.state.enroll_failures += 1
            s = self.settings
            delay = min(s.enroll_backoff_max_s, s.enroll_backoff_start_s * (2 ** (self.state.enroll_failures - 1)))
            delay *= random.uniform(0.8, 1.2)  # noqa: S311 - jitter, geen cryptografie
            if outcome.retry_after_s is not None:
                delay = max(delay, outcome.retry_after_s)  # nooit eerder dan de Hub vraagt
            self.state.next_enroll_at = now + delay
            return
        self._record("hub.pair_failed", "Koppelen met het koppeltoken definitief mislukt.", code=outcome.code)

    def link_step(self, now: float) -> None:
        doc = self.store.read_safe()
        if doc.get("state") != "gekoppeld":
            return
        st, s = self.state, self.settings
        if not doc.get("confirmed_at") and now >= st.next_confirm_at:
            self._after_hub_call(self.enrollment.confirm(), now)
            st.next_confirm_at = now + 30
        if now >= st.next_self_check_at:
            self._after_hub_call(self.enrollment.check_revoked(), now)
            st.next_self_check_at = now + s.hub_self_check_s
        if self.enrollment.rotation_pending() and now >= st.next_rotation_at:
            outcome = self.enrollment.rotate_service_token()
            st.next_rotation_at = now + (0 if outcome.ok else 60)
            if outcome.ok:
                self._record(
                    "hub.token_rotated", "Service-token geroteerd op verzoek van de Hub.", "service.token.rotated"
                )
            else:
                self._after_hub_call(outcome, now)
        if now >= st.next_key_poll_at:
            outcome = self.enrollment.poll_key_rotations()
            st.next_key_poll_at = now + min(s.key_rotation_poll_s, 900)
            if outcome.code == "rotatie_toegepast":
                applied = int(outcome.detail.get("applied") or 0)
                self._record(
                    "hub.key_rotated",
                    "Hub-sleutels bijgewerkt via een ondertekende rotatie.",
                    "service.key_rotation.applied",
                    {"toegepast": applied, "rotation_sequence": outcome.detail.get("rotation_sequence")},
                    **outcome.detail,
                )
            elif outcome.code == "sleutelrotatie_ongeldig":
                self._record(
                    "hub.key_rotation_rejected",
                    "Een sleutelrotatie van de Hub was ongeldig en is niet toegepast.",
                    "service.key_rotation.rejected",
                    {"rotation_sequence": outcome.detail.get("blocked_sequence"), "ernst": "hoog"},
                )

    def _after_hub_call(self, _outcome: Outcome | None = None, _now: float | None = None) -> None:
        """Eén keer per intrekking een audit-regel + incident-event (EX16: geen open toegang, wel een melding)."""
        state = self.store.read_safe().get("state")
        if state == "gekoppeld":
            self.state.revocation_recorded = False
        elif state == "ingetrokken" and not self.state.revocation_recorded:
            self.state.revocation_recorded = True
            self._record(
                "hub.revoked",
                "De Hub heeft de koppeling ingetrokken; Hub-JWT's worden vanaf nu geweigerd.",
                "service.hub_link.revoked",
                {"ernst": "hoog"},
            )

    def push_step(self) -> None:
        doc = self.store.read_safe()
        if doc.get("state") != "gekoppeld" or not doc.get("service_token"):
            return  # niet gekoppeld: events blijven 'pending' (geen poging geteld); de Hub kan ook pollen
        with self.db.sessions() as s:
            for ev in events.due_for_push(s):
                env = events.envelope(ev)
                problems = events.validate_envelope(env)
                if problems:  # zou de inbox weigeren (§7): niet versturen, direct zichtbaar als dead-letter
                    events.mark_invalid(ev, problems)
                    log.error("event %s niet verstuurd: %s", ev.event_id, "; ".join(problems))
                    continue
                try:
                    resp = self.enrollment.authed_request("POST", EVENTS_INBOX_PATH, json=env)
                    if resp is None:
                        break  # intussen ontkoppeld of ingetrokken
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
                if not self._linked():
                    break
            s.commit()
        self._after_hub_call()

    def housekeeping_step(self, now: float) -> None:
        with self.db.sessions() as s:
            self.state.stats["expired_consents"] += expire_consents(s, self.settings)
            self.state.stats["revoked_resweeps"] += sweep_revoked(s)
            n = 0
            for model in (SearchQuery, Transcript):
                res = s.execute(delete(model).where(model.expires_at <= utcnow()))
                n += int(getattr(res, "rowcount", 0) or 0)
            self.state.stats["expired_rows"] += n
            res = s.execute(delete(JwtReplay).where(JwtReplay.expires_at < utcnow()))
            self.state.stats["expired_replay"] += int(getattr(res, "rowcount", 0) or 0)
            if now - self.state.last_purge > 3600:
                purge_expired(s)
                self.state.last_purge = now
            s.commit()

    def heartbeat(self, failed: int) -> None:
        """Hartslag voor de connector-health (``worker``/``jobs``, contract 1.3 §6): tijd van de laatste afgeronde ronde
        en het aantal mislukte stappen daarin. Zonder database geen hartslag (dan wordt de meting vanzelf verouderd)."""
        at = iso(utcnow())
        value = {"laatste_hartslag": at, "stappen_mislukt": failed, "interval_s": self.settings.worker_interval_s}
        with self.db.sessions() as s:
            stmt = insert(ServiceState).values(key=WORKER_HEARTBEAT_KEY, value=value, updated_at=utcnow())
            s.execute(stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value, "updated_at": utcnow()}))
            s.commit()

    def run_once(self) -> None:
        now = time.monotonic()
        failed = 0
        for step in (
            self.startup,
            lambda: self.enroll_step(now),
            lambda: self.link_step(now),
            self.push_step,
            lambda: self.housekeeping_step(now),
        ):
            try:
                step()
            except Exception:  # een fout in één stap mag de andere niet blokkeren
                failed += 1
                log.exception("workerstap mislukt")
        try:
            self.heartbeat(failed)
        except Exception:
            log.warning("hartslag van de worker kon niet worden vastgelegd (database niet beschikbaar)")

    def run(self, stop: threading.Event) -> None:
        log.info(
            "worker gestart (interval %.0f s); ingest is in deze versie niet actief", self.settings.worker_interval_s
        )
        while not stop.is_set():
            self.run_once()
            stop.wait(self.settings.worker_interval_s)
        self.db.dispose()
        log.info("worker gestopt")
