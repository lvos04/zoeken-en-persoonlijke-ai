"""Zoeken en persoonlijke AI — dienst van TrueNAS Home Platform V2.

Modules (elk met duidelijke grenzen):
- ``search`` B07 persoonlijke zoekmachine: bronnen, toestemming (consent), lees-padenallowlist, gebruikers-ACL,
  index (documenten, chunks, embeddings via pgvector) en opschonen bij intrekking.
- ``voice``  B15 spraak/persoonlijke AI: datamodel in P0; STT/TTS/LLM zijn optionele, standaard uitgeschakelde apps.

Niet-gebouwde functies geven eerlijk ``501 capability_unavailable``; nooit gesimuleerd succes.
"""

from importlib import metadata

APP_ID = "zoeken-en-persoonlijke-ai"
SCHEMA_VERSION = "1.0"
SEARCH_PREFIX = "/search/v1"
VOICE_PREFIX = "/voice/v1"
CONNECTOR_PREFIX = "/connector/v1"
# Prefix waaronder de eigen beheer-API (koppeling e.d.) valt; de zoek-API heeft zijn eigen prefix (contract §5).
API_PREFIX = SEARCH_PREFIX

try:
    __version__ = metadata.version("zoeken-en-persoonlijke-ai")
except metadata.PackageNotFoundError:  # pragma: no cover - alleen bij draaien zonder installatie
    __version__ = "0.1.0"
