# syntax=docker/dockerfile:1.7
# Zoeken en persoonlijke AI — ALLES-IN-ÉÉN image (PostgreSQL 16 + pgvector + API/statuspagina + worker).
#
#   docker build -f deploy/allinone/Dockerfile [--secret id=ca_bundle,src=<ca.crt>] -t zoeken-en-persoonlijke-ai:dev .
#
# Installatie uitsluitend als standaard TrueNAS Custom App (zie deploy/truenas/CUSTOM_APP.md): image + tag,
# één poort 30450 → 30450, één opslag → /appdata, optioneel shares als Host Path met 'Read Only' onder /bronnen.
# Geen YAML, geen wachtwoorden, geen verplichte variabelen.
# PostgreSQL draait binnen de container op een unix-socket (geen netwerkpoort, geen wachtwoord) met pgvector 0.8.7
# uit de officiële pgvector-image (exacte versie + sha256-digest, contract §1).
# Start als root alleen om /appdata aan 568:568 te geven; daarna draait alles als 568 (TrueNAS 'apps'), ook PID 1:
# de procesbewaker is zelf PID 1 (geen root-init), zet PR_SET_NO_NEW_PRIVS en ruimt zombies op (contract 1.2 §2.2).
# /bronnen wordt NOOIT gewijzigd (geen chown/chmod): de dienst leest daar alleen.
# De optionele BuildKit-secret 'ca_bundle' is alleen voor bouwomgevingen achter een TLS-proxy; zonder die secret
# bouwt de image schoon (bv. in GitHub Actions).

ARG PYTHON_IMAGE=mirror.gcr.io/library/python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
ARG PGVECTOR_IMAGE=mirror.gcr.io/pgvector/pgvector:0.8.7-pg16-bookworm@sha256:0dd3149a06e4579a0d70d8fef9945636cd514005e7237deb27f9b67886f8dc4f

# ---------------------------------------------------------------- PostgreSQL 16 + pgvector (server + client)
FROM ${PGVECTOR_IMAGE} AS pg
RUN set -eu; mkdir -p /out/lib /out/pg; \
    test -f /usr/lib/postgresql/16/lib/vector.so; \
    test -f /usr/share/postgresql/16/extension/vector.control; \
    grep -q "default_version = '0.8.7'" /usr/share/postgresql/16/extension/vector.control; \
    rm -f /usr/lib/postgresql/16/lib/llvmjit*.so; rm -rf /usr/lib/postgresql/16/lib/bitcode; \
    for f in /usr/lib/postgresql/16/bin/* /usr/lib/postgresql/16/lib/*.so; do ldd "$f" 2>/dev/null || true; done \
      | awk '/=>/ {print $3}' | sort -u \
      | grep -vE '/(libc|libm|libdl|libpthread|librt|libresolv|ld-linux[^/]*)\.so' \
      | while read -r lib; do cp -L "$lib" /out/lib/; done; \
    cp -rL /usr/lib/postgresql/16 /out/pg/lib; cp -rL /usr/share/postgresql/16 /out/pg/share; \
    test -f /out/pg/share/postgresql.conf.sample

# ---------------------------------------------------------------- Python-dienst
FROM ${PYTHON_IMAGE} AS core
WORKDIR /src
COPY apps/api/requirements.lock apps/api/pyproject.toml ./
COPY apps/api/src ./src
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -s /run/secrets/ca_bundle ]; then export PIP_CERT=/run/secrets/ca_bundle; fi; \
    pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore --prefix=/install \
        -r requirements.lock \
    && pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore --prefix=/install \
        --no-deps .

# ---------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE}
ARG VERSION=0.1.0
LABEL org.opencontainers.image.title="zoeken-en-persoonlijke-ai" \
      org.opencontainers.image.description="Zoeken en persoonlijke AI — persoonlijke zoekmachine (PostgreSQL + pgvector) en spraak, alles-in-één" \
      org.opencontainers.image.source="https://github.com/lvos04/zoeken-en-persoonlijke-ai" \
      org.opencontainers.image.version="${VERSION}"
COPY --from=pg /out/pg/lib /usr/lib/postgresql/16
COPY --from=pg /out/pg/share /usr/share/postgresql/16
COPY --from=pg /out/lib /opt/pglibs
COPY --from=core /install /usr/local
COPY apps/api/alembic /usr/local/lib/search-ai/alembic
COPY deploy/allinone/supervisor.py /usr/local/lib/search-ai/supervisor.py
COPY deploy/allinone/healthcheck.py /usr/local/lib/search-ai/healthcheck.py
# statuspagina: versie-gepinde kopie van het Peil-sjabloon met herkomstkop (O-14); de app controleert de sha256
COPY design/ontwerptaal/statuspagina.html /usr/local/lib/search-ai/peil/statuspagina.html
RUN set -eu; \
    for b in postgres initdb pg_ctl pg_isready psql pg_dump pg_restore; do \
      printf '#!/bin/sh\nLD_LIBRARY_PATH=/opt/pglibs exec /usr/lib/postgresql/16/bin/%s "$@"\n' "$b" > /usr/local/bin/$b; \
      chmod 0755 /usr/local/bin/$b; \
    done; \
    postgres --version | grep -q ' 16\.'; pg_dump --version | grep -q ' 16\.'; \
    test -f /usr/lib/postgresql/16/lib/vector.so; \
    groupadd -g 568 apps; useradd -u 568 -g 568 -M -d /tmp -s /usr/sbin/nologin apps; \
    mkdir -p /appdata /bronnen; chown 568:568 /appdata; chmod 0755 /bronnen; \
    find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true; \
    python -c "import search_ai.app, psycopg, uvicorn, cryptography"; \
    python -c "from pathlib import Path; from search_ai.peil import load_template; load_template(Path('/usr/local/lib/search-ai/peil'))"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    SEARCH_AI_DATA_DIR=/appdata SEARCH_AI_PORT=30450 SEARCH_AI_ALEMBIC_DIR=/usr/local/lib/search-ai/alembic \
    SEARCH_AI_PEIL_DIR=/usr/local/lib/search-ai/peil \
    SEARCH_AI_READ_ROOTS=/bronnen \
    SEARCH_AI_DATABASE_URL="postgresql+psycopg://search_ai@/search_ai?host=/tmp/search-ai-pg" SEARCH_AI_VERSION=${VERSION}
VOLUME ["/appdata"]
EXPOSE 30450
HEALTHCHECK --interval=30s --timeout=8s --start-period=90s --retries=3 \
  CMD ["python", "/usr/local/lib/search-ai/healthcheck.py"]
# Geen ENTRYPOINT-init: de bewaker is PID 1 en wisselt zelf naar 568 (zie supervisor.py).
CMD ["python", "/usr/local/lib/search-ai/supervisor.py"]
