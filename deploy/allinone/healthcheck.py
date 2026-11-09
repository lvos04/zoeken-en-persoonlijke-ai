"""Healthcheck van de alles-in-één image: /readyz moet 200 geven (database, migraties, pgvector en geheimen).

Docker start de healthcheck als ``docker exec`` met de gebruiker van de image (root, omdat de image als root start voor
de eigenaarschapsstap). Daarom zakt dit proces als allereerste naar 568:568 met ``no_new_privs`` (contract 1.2 §2.2): ook
de healthcheck draait nooit met rootrechten. Daarna alleen een HTTP-verzoek naar de eigen poort.
"""

import ctypes
import os
import sys
import urllib.request

UID = int(os.environ.get("SEARCH_AI_UID", "568"))
GID = int(os.environ.get("SEARCH_AI_GID", "568"))

try:
    if os.getuid() == 0:
        os.setgroups([])
        os.setgid(GID)
        os.setuid(UID)
    ctypes.CDLL(None, use_errno=True).prctl(38, 1, 0, 0, 0)  # PR_SET_NO_NEW_PRIVS
except (OSError, AttributeError):
    sys.exit(1)  # nooit als root doorgaan

port = os.environ.get("SEARCH_AI_PORT", "30450")
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=6) as resp:
        sys.exit(0 if resp.status == 200 else 1)
except Exception:
    sys.exit(1)
