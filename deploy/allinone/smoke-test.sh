#!/usr/bin/env bash
# Rooktest van de alles-in-één image (ALLEEN ontwikkeling/CI; geen installatiemethode).
#   deploy/allinone/smoke-test.sh [image]   (standaard zoeken-en-persoonlijke-ai:dev)
# Controleert: start als root op een lege /appdata → processen als 568, healthcheck 'healthy', /healthz, /readyz (incl.
# pgvector 0.8.7), pgvector werkt echt (HNSW-index + KNN-zoekvraag), statuspagina 'niet gekoppeld', geen PostgreSQL-
# netwerkpoort en socket alleen voor 568, geheimen 0600/568, databasegegevens en geheimen blijven na herstart, harde
# kill én een nieuwe container op dezelfde /appdata (zoals bij een update), start als 568, duidelijke fouten bij
# verkeerde maprechten en een verkeerde gebruiker, dat een alleen-lezen share onder /bronnen NIET wordt aangeraakt, en
# dat de bewaker als root nooit een symbolische koppeling volgt (in /appdata of als socketmap in /tmp).
# Zoals het standaard TrueNAS Custom App-formulier start de test ZONDER '--security-opt no-new-privileges' en ZONDER
# '--cap-drop': de image moet zelf naar 568 wisselen en no_new_privs zetten. Voor ELK proces in de container, ook
# PID 1, geldt daarna Uid 568, CapEff 0000000000000000 en NoNewPrivs 1 (contract 1.2 §2.2/§10, ADR-0011). Alleen
# 'docker exec'-sessies van buitenaf (PPid 0, zoals deze controle zelf en de Docker-healthcheck) tellen niet mee.
# De test werkt met elke host-gebruiker (ook de niet-root 'runner' van GitHub Actions): /appdata/secrets is bewust
# 0700/568, dus alles daaronder wordt bekeken via een wegwerpcontainer met een alleen-lezen mount (ADR-0009).
set -euo pipefail

IMAGE="${1:-zoeken-en-persoonlijke-ai:dev}"
WORK="$(mktemp -d)"
NAME="sai-smoke-$$"
PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
PASS=0
FAIL=0

cleanup() {
  docker rm -f "$NAME" "$NAME-568" "$NAME-bad" "$NAME-uid" "$NAME-link" >/dev/null 2>&1 || true
  docker run --rm -v "$WORK:/w" --entrypoint sh "$IMAGE" -c 'rm -rf /w/* /w/.[!.]* 2>/dev/null || true' >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

ok() { echo "OK   $*"; PASS=$((PASS + 1)); }
nok() { echo "FOUT $*"; FAIL=$((FAIL + 1)); }
check() { if eval "$2"; then ok "$1"; else nok "$1"; fi; }

wait_healthy() {
  local c="$1"
  for _ in $(seq 1 60); do
    case "$(docker inspect -f '{{.State.Health.Status}}' "$c" 2>/dev/null || echo weg)" in
      healthy) return 0 ;;
      weg) return 1 ;;
    esac
    sleep 3
  done
  docker logs "$c" | tail -30
  return 1
}

# zonder pipes (pipefail + grep -q geeft anders vals-negatieve SIGPIPE-fouten)
http_has() { curl -fsS "http://127.0.0.1:$PORT$1" -o "$WORK/http.txt" && grep -q -- "$2" "$WORK/http.txt"; }
log_has() { docker logs "$1" > "$WORK/log.txt" 2>&1; grep -q -- "$2" "$WORK/log.txt"; }
# wacht (hooguit 30 s) tot een melding in de log staat, i.p.v. een vaste slaaptijd
wait_log() { for _ in $(seq 1 30); do log_has "$1" "$2" && return 0; sleep 1; done; docker logs "$1" 2>&1 | tail -10; return 1; }

# Bestanden op de host-map bekijken zoals ze op schijf staan, via een wegwerpcontainer (root in de container,
# alleen-lezen mount). Nodig omdat /appdata/secrets 0700/568 is: de host-gebruiker mag daar terecht niet in.
on_disk() { docker run --rm --network none -v "$WORK:/w:ro" --entrypoint "$1" "$IMAGE" "${@:2}"; }
disk_mode() { on_disk stat -c '%a %u' "/w/$1"; }
disk_sum() { on_disk sha256sum "/w/$1" | cut -d' ' -f1; }

# psql in de container als 568 (de enige gebruiker die de unix-socket mag gebruiken)
sql() { local c="$1"; shift; docker exec -u 568 -i "$c" psql -h /tmp/search-ai-pg -U search_ai -d search_ai -v ON_ERROR_STOP=1 -tAq "$@"; }

# pgvector echt gebruiken in de ingebouwde database: HNSW-index bouwen en een KNN-zoekvraag via die index
# (in een transactie die wordt teruggedraaid: er blijft niets achter)
pgvector_works() {
  sql "$1" > "$WORK/vec.txt" 2>&1 <<'SQL' || return 1
BEGIN;
CREATE TABLE rooktest_vec (id int PRIMARY KEY, v vector(3) NOT NULL);
INSERT INTO rooktest_vec SELECT g, ARRAY[random() + 0.01, random(), random()]::vector(3) FROM generate_series(1, 500) g;
INSERT INTO rooktest_vec VALUES (0, '[1,0,0]');
CREATE INDEX rooktest_vec_hnsw ON rooktest_vec USING hnsw (v vector_cosine_ops);
SET LOCAL enable_seqscan = off;
SELECT 'knn=' || id FROM rooktest_vec ORDER BY v <=> '[1,0,0]' LIMIT 1;
EXPLAIN SELECT id FROM rooktest_vec ORDER BY v <=> '[1,0,0]' LIMIT 1;
SELECT 'dims=' || vector_dims('[1,2,3]'::vector);
ROLLBACK;
SQL
  grep -qx 'knn=0' "$WORK/vec.txt" && grep -q 'Index Scan using rooktest_vec_hnsw' "$WORK/vec.txt" \
    && grep -qx 'dims=3' "$WORK/vec.txt"
}

# Contract 1.2 §2.2: elk proces van de container (ook PID 1) als 568 (echte én effectieve uid), zonder effectieve
# capabilities en met no_new_privs. Uitgezonderd: 'docker exec'-sessies (PPid 0, behalve PID 1) en hun kinderen.
privs_all_568() {
  docker exec "$1" python -c '
import os
me = os.getpid()
ok = bad = 0
for p in filter(str.isdigit, os.listdir("/proc")):
    try:
        lines = open(f"/proc/{p}/status").read().splitlines()
    except OSError:
        continue
    f = {k: v.split() for k, _, v in (ln.partition(":") for ln in lines)}
    ppid = f.get("PPid", ["0"])[0]
    if int(p) == me or int(ppid) == me or (ppid == "0" and p != "1"):
        continue
    good = (f.get("Uid", [""])[:2] == ["568", "568"] and f.get("CapEff") == ["0000000000000000"]
            and f.get("NoNewPrivs") == ["1"])
    ok += good
    bad += not good
    if not good:
        print("FOUT proces", p, f.get("Name"), f.get("Uid"), f.get("CapEff"), f.get("NoNewPrivs"))
raise SystemExit(0 if ok >= 4 and bad == 0 else 1)
'
}

# PID 1 is de bewaker zelf (geen dumb-init/tini die als root blijft draaien)
pid1_is_supervisor() {
  docker exec "$1" python -c '
cmd = open("/proc/1/cmdline", "rb").read().split(b"\0")
st = {k: v.split() for k, _, v in (ln.partition(":") for ln in open("/proc/1/status").read().splitlines())}
ok = any(c.endswith(b"supervisor.py") for c in cmd) and st["Uid"][0] == "568" and st["CapEff"] == ["0000000000000000"]
raise SystemExit(0 if ok else 1)
'
}

# PID 1 ruimt verweesde processen op: een achtergrondproces van een exec-sessie wordt kind van PID 1
no_zombies_after_orphan() {
  docker exec -u 568 "$1" sh -c 'sleep 1 & exit 0'
  sleep 6
  docker exec "$1" python -c '
import os
z = []
for p in filter(str.isdigit, os.listdir("/proc")):
    try:
        if open(f"/proc/{p}/stat").read().rsplit(")", 1)[1].split()[0] == "Z":
            z.append(p)
    except OSError:
        pass
raise SystemExit(1 if z else 0)
'
}

# markering in de echte database (service_state) om dataverlies na herstart/nieuwe container te zien
MARKER="rooktest-$$-$RANDOM"
marker_set() { sql "$1" -c "INSERT INTO service_state (key, value, updated_at) VALUES ('rooktest', '{\"m\": \"$MARKER\"}', now())" >/dev/null; }
marker_ok() { [ "$(sql "$1" -c "SELECT value->>'m' FROM service_state WHERE key = 'rooktest'")" = "$MARKER" ]; }

run() { # naam, datamap, extra args (geen --security-opt: dat kan het TrueNAS-formulier ook niet)
  local n="$1" d="$2"; shift 2
  docker run -d --name "$n" -p "127.0.0.1:$PORT:30450" -v "$d:/appdata" \
    --health-interval 3s --health-start-period 60s "$@" "$IMAGE" >/dev/null
}

mkdir -p "$WORK/root" "$WORK/u568" "$WORK/bad" "$WORK/u1000" "$WORK/link" "$WORK/share/werk"
echo "offerte keuken" > "$WORK/share/werk/offerte.txt"
# root-eigendom-map zoals een nieuwe TrueNAS-dataset; 'bad' is van root (0755) zodat 568 er nooit in mag schrijven
# en 'u1000' is van 1000 — ongeacht de uid van de host-gebruiker die de test draait
docker run --rm -v "$WORK:/w" --entrypoint sh "$IMAGE" -c \
  'chown -R 1000:1000 /w/share /w/u1000 && chown 0:0 /w/root /w/bad && chmod 0755 /w/root /w/bad' >/dev/null
SHARE_BEFORE="$(cd "$WORK/share" && find . -printf '%p %u %g %m %s %T@\n' | sort | sha256sum)"

echo "== 1. start als root op een root-eigendom-map, met een alleen-lezen share onder /bronnen"
run "$NAME" "$WORK/root" -v "$WORK/share:/bronnen/documenten:ro"
check "healthcheck wordt healthy" "wait_healthy $NAME"
check "/healthz geeft 200" "http_has /healthz '\"status\":\"ok\"'"
check "/readyz geeft ready" "http_has /readyz '\"ready\"'"
check "/readyz meldt pgvector ok" "http_has /readyz '\"pgvector\"'"
check "pgvector 0.8.7 in de ingebouwde database" \
  "[ \"\$(docker exec -u 568 $NAME psql -h /tmp/search-ai-pg -U search_ai -d search_ai -tAc \"SELECT extversion FROM pg_extension WHERE extname='vector'\")\" = '0.8.7' ]"
check "pgvector werkt echt (HNSW-index, KNN via de index, vector_dims)" "pgvector_works $NAME"
check "migraties op de nieuwste revisie (samengestelde bron-sleutels aanwezig)" \
  "[ \"\$(sql $NAME -c \"SELECT count(*) FROM pg_constraint WHERE conname = 'fk_embedding_chunk_id_chunk' AND array_length(conkey, 1) = 2\")\" = 1 ]"
check "statuspagina toont 'niet gekoppeld'" "http_has / 'niet gekoppeld'"
check "API zonder token geeft 401 unauthorized" \
  "[ \"\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORT/search/v1/sources)\" = 401 ]"
docker top "$NAME" -eo pid,user,args > "$WORK/ps.txt"
check "procestabel is leesbaar" "[ \"\$(wc -l < $WORK/ps.txt)\" -ge 5 ]"
check "geen enkel proces draait als root, ook PID 1 niet (alleen de Docker-healthcheck start even als root)" \
  "[ -z \"\$(awk 'NR>1 && \$2!=\"568\" && \$2!=\"apps\" && \$0 !~ /healthcheck.py/' $WORK/ps.txt)\" ]"
check "PID 1 is de bewaker zelf, als 568 zonder capabilities (geen root-init)" "pid1_is_supervisor $NAME"
check "postgres, api en worker draaien als 568" \
  "[ \"\$(awk '(\$2==\"568\" || \$2==\"apps\") && /postgres -D|search_ai api|search_ai worker/' $WORK/ps.txt | wc -l)\" -ge 3 ]"
check "elk proces (ook PID 1): Uid 568, CapEff 0 en NoNewPrivs 1, zonder --security-opt/--cap-drop" \
  "privs_all_568 $NAME"
check "PID 1 ruimt verweesde processen (zombies) op" "no_zombies_after_orphan $NAME"
check "geen netwerkpoort voor PostgreSQL (alleen unix-socket)" \
  "! docker exec $NAME python -c 'import socket;s=socket.socket();s.settimeout(1);s.connect((\"127.0.0.1\",5432))' 2>/dev/null"
check "database-socket alleen voor gebruiker 568 (rechten 0700)" \
  "[ \"\$(docker exec $NAME stat -c '%a %u' /tmp/search-ai-pg/.s.PGSQL.5432)\" = '700 568' ]"
check "andere gebruiker in de container kan de database niet bereiken" \
  "! docker exec -u nobody $NAME psql -h /tmp/search-ai-pg -U search_ai -d search_ai -tAc 'SELECT 1' > $WORK/nobody.txt 2>&1 && grep -q 'Permission denied' $WORK/nobody.txt"
check "secrets-map 0700 en eigenaar 568" "[ \"\$(disk_mode root/secrets)\" = '700 568' ]"
check "geheimen 0600 en eigenaar 568" \
  "[ \"\$(disk_mode root/secrets/vault.key)\" = '600 568' ] && [ \"\$(disk_mode root/secrets/identity.ed25519)\" = '600 568' ]"
check "geen lokale ontkoppelcode meer in de app-log (contract 1.2 §3.7)" "! log_has $NAME 'ontkoppelcode'"
check "nog niet gekoppeld: geen hub-link.json" "! on_disk test -e /w/root/secrets/hub-link.json"
check "share wordt als alleen-lezen gemeld" "log_has $NAME 'documenten: alleen-lezen'"
check "share ongewijzigd (eigenaar, rechten, inhoud, tijden)" \
  "[ \"\$(cd $WORK/share && find . -printf '%p %u %g %m %s %T@\n' | sort | sha256sum)\" = '$SHARE_BEFORE' ]"
check "geen setuid/setgid-bestanden in de image" \
  "[ -z \"\$(docker exec $NAME sh -c 'find / -xdev -perm /6000 -type f 2>/dev/null')\" ]"
SUM1="$(disk_sum root/secrets/vault.key)" || SUM1=""
check "vault.key leesbaar voor de controle (via container)" "[ \${#SUM1} -eq 64 ]"
check "markering in de database geschreven" "marker_set $NAME && marker_ok $NAME"

echo "== 2. herstart: data en geheimen blijven"
docker restart -t 30 "$NAME" >/dev/null
check "SIGTERM komt aan bij PID 1: netjes gestopt" "log_has $NAME 'netjes stoppen' && log_has $NAME 'gestopt'"
check "na herstart weer healthy" "wait_healthy $NAME"
check "vault.key ongewijzigd na herstart" "[ -n '$SUM1' ] && [ \"\$(disk_sum root/secrets/vault.key)\" = '$SUM1' ]"
check "databasegegevens behouden na herstart" "marker_ok $NAME"

echo "== 3. harde kill (stroomuitval) en opnieuw starten"
docker kill "$NAME" >/dev/null
docker start "$NAME" >/dev/null
check "na harde kill weer healthy" "wait_healthy $NAME"
check "databasegegevens behouden na harde kill" "marker_ok $NAME"
docker rm -f "$NAME" >/dev/null

echo "== 3b. nieuwe container op dezelfde /appdata (zoals TrueNAS bij een update doet)"
run "$NAME" "$WORK/root" -v "$WORK/share:/bronnen/documenten:ro"
check "nieuwe container healthy" "wait_healthy $NAME"
check "databasegegevens behouden in een nieuwe container" "marker_ok $NAME"
check "vault.key ongewijzigd in een nieuwe container" "[ -n '$SUM1' ] && [ \"\$(disk_sum root/secrets/vault.key)\" = '$SUM1' ]"
check "geen nieuwe database aangemaakt (geen 'eerste start' in de log)" "! log_has $NAME 'eerste start: database aanmaken'"
docker rm -f "$NAME" >/dev/null

echo "== 4. start direct als 568 op een 568-map"
docker run --rm -v "$WORK:/w" --entrypoint sh "$IMAGE" -c 'chown 568:568 /w/u568' >/dev/null
run "$NAME-568" "$WORK/u568" --user 568:568
check "als 568 gestart ('Custom User' 568 in het formulier) en healthy" "wait_healthy $NAME-568"
check "ook dan elk proces: Uid 568, CapEff 0, NoNewPrivs 1" "privs_all_568 $NAME-568"
docker rm -f "$NAME-568" >/dev/null

echo "== 5. verkeerde rechten (568 op een root-dataset) geven een duidelijke Nederlandse fout"
docker run -d --name "$NAME-bad" -v "$WORK/bad:/appdata" --user 568:568 "$IMAGE" >/dev/null
check "fout 'geen schrijfrechten' in de log" "wait_log $NAME-bad 'FOUT: geen schrijfrechten'"
check "root-dataset niet aangeraakt" "[ \"\$(disk_mode bad)\" = '755 0' ] && [ -z \"\$(ls -A $WORK/bad)\" ]"
docker rm -f "$NAME-bad" >/dev/null

echo "== 6. verkeerde gebruiker ('Custom User' 1000) geeft een duidelijke Nederlandse fout, geen initdb-fout"
docker run -d --name "$NAME-uid" -v "$WORK/u1000:/appdata" --user 1000:1000 "$IMAGE" >/dev/null
check "fout 'draait als gebruiker 1000:1000' in de log" "wait_log $NAME-uid 'FOUT: de app draait als gebruiker 1000:1000'"
check "geen initdb gestart" "! log_has $NAME-uid 'initdb'"
docker rm -f "$NAME-uid" >/dev/null

echo "== 7. symbolische koppelingen: root volgt ze nooit (anders kan 568 root buiten /appdata laten chownen)"
docker run --rm -v "$WORK:/w" --entrypoint sh "$IMAGE" -c \
  'chown 0:0 /w/link && chmod 0755 /w/link && ln -s /usr/local/lib /w/link/backups' >/dev/null
docker run -d --name "$NAME-link" -v "$WORK/link:/appdata" "$IMAGE" >/dev/null
check "link als eigen submap van /appdata geeft een duidelijke Nederlandse fout" \
  "wait_log $NAME-link 'FOUT: /appdata/backups is een symbolische koppeling'"
check "doel van die link niet aangeraakt (blijft van root)" \
  "[ \"\$(docker exec $NAME-link stat -c '%u' /usr/local/lib)\" = 0 ]"
check "geen database aangemaakt bij een geweigerde start" "! log_has $NAME-link 'initdb'"
docker rm -f "$NAME-link" >/dev/null

echo "== 7b. socketmap in /tmp door 568 vervangen door een link, daarna herstarten (root chownt het doel niet)"
run "$NAME" "$WORK/root" -v "$WORK/share:/bronnen/documenten:ro"
check "container healthy vóór de proef" "wait_healthy $NAME"
docker exec -u 568 "$NAME" sh -c 'mv /tmp/search-ai-pg /tmp/search-ai-pg.oud && ln -s /usr/local/lib /tmp/search-ai-pg'
docker restart -t 30 "$NAME" >/dev/null
check "na herstart weer healthy" "wait_healthy $NAME"
check "doel van de link niet aangeraakt (blijft van root)" \
  "[ \"\$(docker exec $NAME stat -c '%u' /usr/local/lib)\" = 0 ]"
check "socketmap is weer een gewone map van 568" \
  "[ \"\$(docker exec $NAME stat -c '%F %u' /tmp/search-ai-pg)\" = 'directory 568' ]"
check "databasegegevens behouden" "marker_ok $NAME"
check "ook daarna elk proces: Uid 568, CapEff 0, NoNewPrivs 1" "privs_all_568 $NAME"
docker rm -f "$NAME" >/dev/null

echo
echo "Resultaat: $PASS geslaagd, $FAIL mislukt"
[ "$FAIL" -eq 0 ]
