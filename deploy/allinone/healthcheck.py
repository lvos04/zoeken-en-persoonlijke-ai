"""Healthcheck van de alles-in-één image: /readyz moet 200 geven (database, migraties, pgvector en geheimen).

Docker start de healthcheck (en ``docker exec``) met de gebruiker van de image, buiten de procesboom van PID 1: zonder
"Custom User" dus als root, met de standaard-capabilities en zonder no_new_privs. Daarom is dit programma in exec-vorm
(geen shell) en doet het als **allereerste** handeling (contract 1.3 §2.2): bij root de bounding set legen, dan
``setgroups([])``, ``setgid(568)``, ``setuid(568)`` en ``prctl(PR_SET_NO_NEW_PRIVS, 1)``. Mislukt dat, dan exit 1 (rood):
nooit als root doorgaan. Daarna alleen een HTTP-verzoek naar de eigen poort.

Controlevlag voor de rooktest: ``healthcheck.py --controle`` doet dezelfde wissel en meldt daarna ``Uid``, ``CapEff``,
``CapPrm``, ``CapAmb``, ``CapBnd`` en ``NoNewPrivs`` (exit 0 alleen als Uid 568, CapEff/CapPrm/CapAmb 0, NoNewPrivs 1 en,
bij een start als root, CapBnd 0), zonder HTTP-verzoek.
"""

import ctypes
import errno
import os
import sys
import urllib.request

UID = int(os.environ.get("SEARCH_AI_UID", "568"))
GID = int(os.environ.get("SEARCH_AI_GID", "568"))
PR_SET_NO_NEW_PRIVS = 38
PR_CAPBSET_DROP = 24
EMPTY = "0000000000000000"


def status() -> dict[str, str]:
    with open("/proc/self/status", encoding="ascii") as fh:
        return {k: v.strip() for k, _, v in (line.partition(":") for line in fh)}


def drop_privileges() -> bool:
    """Geeft ``True`` als dit proces als root gestart is (dan moet de bounding set leeg zijn)."""
    libc = ctypes.CDLL(None, use_errno=True)
    started_as_root = os.getuid() == 0
    if started_as_root:
        try:
            with open("/proc/sys/kernel/cap_last_cap", encoding="ascii") as fh:
                last = int(fh.read().strip())
        except (OSError, ValueError):
            last = 63
        for cap in range(last + 1):
            if libc.prctl(PR_CAPBSET_DROP, cap, 0, 0, 0) != 0 and ctypes.get_errno() != errno.EINVAL:
                raise OSError(ctypes.get_errno(), "bounding set legen mislukt")
        os.setgroups([])
        os.setgid(GID)
        os.setuid(UID)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "no_new_privs mislukt")
    return started_as_root


try:
    root_start = drop_privileges()
except (OSError, AttributeError, ValueError):
    sys.exit(1)  # nooit als root doorgaan

if "--controle" in sys.argv[1:]:
    st = status()
    velden = ("Uid", "Gid", "CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb", "NoNewPrivs")
    for veld in velden:
        print(f"{veld}: {st.get(veld, '?')}")
    ok = (
        st.get("Uid", "").split()[:2] == [str(UID), str(UID)]
        and all(st.get(c) == EMPTY for c in ("CapPrm", "CapEff", "CapAmb"))
        and st.get("NoNewPrivs") == "1"
        and (not root_start or st.get("CapBnd") == EMPTY)
    )
    print(f"gestart als root: {'ja' if root_start else 'nee'} · controle: {'goed' if ok else 'FOUT'}")
    sys.exit(0 if ok else 1)

port = os.environ.get("SEARCH_AI_PORT", "30450")
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=6) as resp:
        sys.exit(0 if resp.status == 200 else 1)
except Exception:
    sys.exit(1)
