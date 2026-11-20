#!/usr/bin/env bash
# Elke job krijgt een eigen pgvector-container en netwerk op de buildengine.
set -euo pipefail

naamfile="$RUNNER_TEMP/zoek-testdatabase"
if [[ ${1:-} == stop ]]; then
  if [[ -s $naamfile ]]; then
    naam=$(cat "$naamfile")
    docker rm -fv "$naam" >/dev/null
    docker network rm "$naam" >/dev/null
  fi
  exit 0
fi

naam="zoek-pg-$(cat /proc/sys/kernel/random/uuid)"
image=mirror.gcr.io/pgvector/pgvector:0.8.7-pg16-bookworm@sha256:0dd3149a06e4579a0d70d8fef9945636cd514005e7237deb27f9b67886f8dc4f
loopback=$(getent ahostsv4 localhost | awk 'NR == 1 {print $1}')
[[ $loopback == 127.* ]]
docker network create "$naam" >/dev/null
if ! docker run -d --name "$naam" --network "$naam" --publish "$loopback::5432" \
    -e POSTGRES_HOST_AUTH_METHOD=trust "$image" postgres -c "cluster_name=$naam" >/dev/null; then
  docker rm -fv "$naam" >/dev/null 2>&1 || true
  docker network rm "$naam" >/dev/null
  exit 1
fi
echo "$naam" > "$naamfile"

poort=$(docker port "$naam" 5432/tcp)
export PGHOST=localhost PGPORT="${poort##*:}" PGUSER=postgres
for _ in {1..45}; do
  if [[ $(psql -XAtqd postgres -c 'SHOW cluster_name' 2>/dev/null) == "$naam" ]]; then
    psql -Xd postgres -v ON_ERROR_STOP=1 -c 'CREATE EXTENSION vector'
    {
      echo "PGHOST=$PGHOST"
      echo "PGPORT=$PGPORT"
      echo "PGUSER=$PGUSER"
      echo "SEARCH_AI_TEST_ADMIN_URL=postgresql+psycopg://postgres@localhost:$PGPORT/postgres"
    } >> "$GITHUB_ENV"
    exit 0
  fi
  sleep 1
done
echo 'De eigen testdatabase antwoordde niet met de verwachte clusternaam.' >&2
exit 1
