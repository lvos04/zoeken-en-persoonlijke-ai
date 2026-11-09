# ADR-0009 — Rooktest werkt met elke host-gebruiker; duidelijke fout bij een verkeerde run-gebruiker

Status: aanvaard · Datum: 2026-11-09 · Fase: P0 (afmaken: CI groen) · Excel: I14-14-02, B07-001

## Aanleiding
De GitHub-job `image`, stap *Starten, healthcheck en rooktest*, faalde bij elke push sinds de eerste versie
(runs 42601471254, 42602000622, 42605815492). De log toont:

```
stat: cannot statx '/tmp/tmp.…/root/secrets/vault.key': Permission denied
FOUT geheimen 0600 en eigenaar 568
sha256sum: /tmp/tmp.…/root/secrets/vault.key: Permission denied
##[error]Process completed with exit code 1.
```

Oorzaak: `deploy/allinone/smoke-test.sh` las `/appdata/secrets/vault.key` op de **host** met `stat` en `sha256sum`,
als de gebruiker die de test draait. De bewaker maakt `/appdata/secrets` bewust `0700` met eigenaar `568` (zie
`supervisor.py`: `os.chmod(DATA / "secrets", 0o700)`). De GitHub-runner (een gewone, niet-root gebruiker) mag die
map dus terecht niet in. Door `set -e` brak de test bij `SUM1="$(sha256sum …)"` daarna af zonder samenvatting.
Lokaal was de test eerder alleen als root gedraaid; daar werkt `stat` ondanks `0700`. Op deze machine als gewone
gebruiker (uid 1000) is de fout precies zo gereproduceerd. Het is dus **geen traagheid of tijdslimiet**, en **geen
fout in de image**: de image doet wat hij moet doen; de test keek op de verkeerde manier.

Bij het reproduceren bleek nog een tweede, verborgen hostafhankelijkheid: stap 5 ("verkeerde rechten") startte de
container als `1000:1000` op een map die van de host-gebruiker is. Op een host waar die gebruiker zelf uid 1000 heeft,
mag de container daar wél schrijven en faalt `initdb` met de Engelse melding *could not look up effective user ID
1000* in plaats van de verwachte Nederlandse fout. Dat is ook een productpunt: een TrueNAS-gebruiker die *Custom User*
aanzet met een andere uid dan 568 kreeg zo'n onduidelijke melding.

## Besluit
1. **Bestanden onder `/appdata` worden in de rooktest via een wegwerpcontainer bekeken** (`on_disk`, `disk_mode`,
   `disk_sum`): `docker run --rm --network none -v "$WORK:/w:ro" …` met `stat`/`sha256sum`. De mount is alleen-lezen
   en zonder netwerk, dus de controle verandert niets. Het toetst hetzelfde (eigenaar, rechten en inhoud op schijf,
   ook na herstart en in een nieuwe container), maar onafhankelijk van wie de test draait. Extra controle: de
   `secrets`-map zelf is `0700/568`. De rechten van `secrets/` worden **niet** verruimd om de test te laten slagen.
2. **Testmappen deterministisch:** `root` en `bad` worden vooraf echt van root (`0755`) gemaakt — `root` zoals een
   nieuwe TrueNAS-dataset (de stap heette al "root-eigendom-map", maar was dat niet), `bad` zodat 568 er nooit in mag
   schrijven. De uitkomst hangt niet meer af van de uid van de host-gebruiker.
3. **Stap 5** test nu het realistische geval: de app als `568:568` op een root-dataset → `FOUT: geen schrijfrechten`
   en de dataset blijft onaangeroerd. **Nieuwe stap 6:** de app als `1000:1000` (*Custom User* met een andere uid) →
   de bewaker stopt vóór de mappen en vóór `initdb` met `FOUT: de app draait als gebruiker 1000:1000 … Zet in de
   app-instellingen 'Custom User' uit … of gebruik gebruiker en groep 568.` (`supervisor.check_run_user`). Alleen root
   (die daarna naar 568 gaat) en precies `568:568` zijn toegestaan; dat was in de praktijk al zo, nu met een duidelijke
   melding.
4. Wachten op een logmelding gebeurt met polling (hooguit 30 s) in plaats van een vaste `sleep 8`; dat maakt de
   test niet trager als het goed gaat en niet vals-negatief op een trage runner. Er is **geen** tijdslimiet opgehoogd.

## Gevolgen
- De rooktest draait identiek als root, als gewone gebruiker en als de GitHub-runner; 37 controles.
- Unittests bewaken `check_run_user` (root, 568:568 toegestaan; 1000:1000 en 568:0 geweigerd met NL-melding) en dat
  de rooktest nooit meer als host-gebruiker in `secrets/` kijkt (`test_deploy_artifacts.py`).
- Los hiervan: de integratietest van de back-up vóór een upgrade gebruikt nu `pg_dump` en `pg_restore` van dezelfde
  hoofdversie als de server. Op een ontwikkelmachine met PostgreSQL-client 16 én 18 koos Debians `pg_wrapper` voor
  `pg_dump` (met host) versie 18 en voor `pg_restore --list` (zonder host) versie 16, die het nieuwere dumpformaat niet
  kent. In de image staat alleen PG 16; de dienst zelf is niet gewijzigd.
- Validatie op een echte NAS blijft **NIET GEVALIDEERD**: het TrueNAS-formulier (*Custom User*) is niet op echte
  TrueNAS doorlopen; het gedrag is in Docker met `--user` nagebootst.
