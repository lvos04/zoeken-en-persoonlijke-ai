"""Healthcheck van de alles-in-één image: /readyz moet 200 geven (database, migraties, pgvector en geheimen)."""

import os
import sys
import urllib.request

port = os.environ.get("SEARCH_AI_PORT", "30450")
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=6) as resp:
        sys.exit(0 if resp.status == 200 else 1)
except Exception:
    sys.exit(1)
