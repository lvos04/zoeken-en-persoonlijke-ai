"""Configuratie uit omgevingsvariabelen.

De standaard TrueNAS Custom App heeft **geen verplichte variabelen**: alles heeft een veilige standaard.
Geheimen staan nooit in de omgeving; die maakt de app zelf aan in ``<data>/secrets`` (zie security.secrets).
Het enrollment-token in de omgeving is eenmalig en na gebruik waardeloos (contract §3).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PORT = 30450
DEFAULT_DATA_DIR = "/appdata"
DEFAULT_DB_URL = "postgresql+psycopg://search_ai@/search_ai?host=/tmp/search-ai-pg"
# Leesbronnen worden als extra Host Path (Read Only) onder deze map gekoppeld (zie deploy/truenas/CUSTOM_APP.md).
DEFAULT_READ_ROOTS = ("/bronnen",)
ENV_PREFIX = "SEARCH_AI_"


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} moet een geheel getal zijn, niet {raw!r}") from exc


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "ja", "yes", "aan", "on")


def _opt(name: str) -> str | None:
    raw = os.environ.get(name, "").strip()
    return raw or None


def _paths(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if not raw:
        return default
    return tuple(p.strip() for p in raw.split(",") if p.strip())


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(DEFAULT_DATA_DIR)
    database_url: str = DEFAULT_DB_URL
    port: int = DEFAULT_PORT
    hub_url: str | None = None
    enrollment_token: str | None = None
    public_url: str | None = None
    alembic_dir: Path | None = None
    # JWT/JWKS (contract §4): iss=hub, aud=<dienst>, exp - iat <= 120 s
    jwt_issuer: str = "hub"
    jwt_audience: str = "zoeken-en-persoonlijke-ai"
    jwt_max_lifetime_s: int = 120
    jwt_leeway_s: int = 30
    jwks_ttl_s: int = 300
    jwks_max_stale_s: int = 900
    jwks_min_refresh_interval_s: int = 30
    # uitgaande calls (contract §5): 5 s connect / 15 s read, max 3 pogingen
    http_connect_timeout_s: float = 5.0
    http_read_timeout_s: float = 15.0
    http_max_attempts: int = 3
    # B07-001: lees-padenallowlist. Alleen paden binnen deze wortels kunnen ooit een bron worden.
    read_roots: tuple[str, ...] = field(default_factory=lambda: DEFAULT_READ_ROOTS)
    # B07-001/B07-002: toestemming alleen voor alleen-lezen koppelingen (ingest wijzigt nooit originelen).
    require_readonly_mounts: bool = True
    # bovengrens voor de bronwizard-voorvertoning (alleen namen tellen, geen inhoud lezen)
    preview_max_files: int = 20000
    worker_interval_s: float = 10.0
    # alleen voor ontwikkeling/tests: een Hub op 127.0.0.1 toestaan (in de container wijst loopback naar zichzelf)
    allow_loopback_hub: bool = False
    # in-app-cache van de koppelstatus (API-proces leest de status die de worker schreef)
    link_cache_ttl_s: float = 5.0
    # B07 §3: verzoeklimiet en tijdlimiet voor zoekopdrachten (per gebruiker, per API-proces)
    search_rate_limit: int = 60
    search_rate_window_s: float = 60.0
    search_statement_timeout_ms: int = 5000

    @property
    def secrets_dir(self) -> Path:
        return self.data_dir / "secrets"

    @classmethod
    def from_env(cls) -> Settings:
        p = ENV_PREFIX
        alembic_dir = _opt(p + "ALEMBIC_DIR")
        return cls(
            data_dir=Path(os.environ.get(p + "DATA_DIR", DEFAULT_DATA_DIR)),
            database_url=os.environ.get(p + "DATABASE_URL", DEFAULT_DB_URL),
            port=_int(p + "PORT", DEFAULT_PORT),
            hub_url=_normalize_url(_opt("HUB_URL")),
            enrollment_token=_opt("HUB_ENROLLMENT_TOKEN") or _opt("ENROLLMENT_TOKEN"),
            public_url=_normalize_url(_opt(p + "PUBLIC_URL")),
            alembic_dir=Path(alembic_dir) if alembic_dir else None,
            jwks_ttl_s=_int(p + "JWKS_TTL_S", 300),
            jwks_max_stale_s=_int(p + "JWKS_MAX_STALE_S", 900),
            read_roots=_paths(_opt(p + "READ_ROOTS"), DEFAULT_READ_ROOTS),
            require_readonly_mounts=_bool(p + "REQUIRE_READONLY_MOUNTS", True),
            preview_max_files=_int(p + "PREVIEW_MAX_FILES", 20000),
            worker_interval_s=float(_int(p + "WORKER_INTERVAL_S", 10)),
            allow_loopback_hub=_bool(p + "ALLOW_LOOPBACK_HUB"),
            search_rate_limit=_int(p + "SEARCH_RATE_LIMIT", 60),
            search_statement_timeout_ms=_int(p + "SEARCH_STATEMENT_TIMEOUT_MS", 5000),
        )


def _normalize_url(url: str | None) -> str | None:
    if url is None:
        return None
    return url.rstrip("/")
