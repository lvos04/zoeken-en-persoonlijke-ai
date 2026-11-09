# ADR-0004 — Lees-padenallowlist, shares als alleen-lezen Host Path en symlinks

Status: aanvaard · Datum: 2026-11-09 · Fase: P0 · Excel: B07-001, I25-25-01

## Besluit
1. **Systeembrede leeswortels** (`SEARCH_AI_READ_ROOTS`, in de image `/bronnen`). Ongeldige wortels (`/`, `/mnt`,
   `/home`, `/var`, `/srv`, `/tmp`, systeemmappen, overlap met `/appdata`) worden genegeerd en gelogd — nooit verruimd.
2. **Shares koppelen** gebeurt als extra *Host Path* met **Read Only** onder `/bronnen/<naam>`. Toestemming wordt
   geweigerd (`bron_niet_klaar`) als een gekozen map niet bereikbaar of niet alleen-lezen is
   (`SEARCH_AI_REQUIRE_READONLY_MOUNTS`, standaard aan). De procesbewaker raakt `/bronnen` nooit aan (geen
   chown/chmod) en meldt per share of hij alleen-lezen is.
3. Na onboarding voegt **de Hub** zo'n opslagregel toe via `app.update` (executor, R2) nadat in de bronwizard een bron
   is gekozen — nooit deze app zelf. De manifeststandaard v1 kan geen herhaalbare/optionele opslagregels uitdrukken;
   daarom bevat het manifest alleen `/appdata` (contract_issue).
4. **Per bron** `allowed_paths` (absoluut, genormaliseerd, geen `.`/`..`, binnen een leeswortel, niet overlappend,
   max. 50) en eigen `exclude_patterns`. **Vaste uitsluitingen** die altijd gelden: `.zfs`, `.snapshot(s)`, `@eaDir`,
   `#recycle`, `.Trash*`, `$RECYCLE.BIN`, `lost+found`, `.ssh`, `.gnupg`, `.aws`, `.azure`, `.kube`, `.docker`,
   `.password-store`, `.git`, `secrets`, `.env*`, `*.key`, `*.pem`, `*.p12`, `*.pfx`, `*.kdbx`, `*.ovpn`,
   `id_rsa*`, `id_ecdsa*`, `id_ed25519*`, `*.gpg` (hoofdletterongevoelig).
5. **Symlinks**: mappen- en bestandssymlinks worden niet gevolgd; elk kandidaatbestand wordt op het echte pad
   (`realpath`) opnieuw tegen het echte toegestane pad gecontroleerd (`symlink_buiten_bron`).
6. De **bronwizard-voorvertoning** gebruikt alleen `lstat`/namen (geen `open`), telt en toont een kleine steekproef
   van relatieve namen aan de eigenaar; hard begrensd (`SEARCH_AI_PREVIEW_MAX_FILES`).
7. Bij een smallere selectie verwijdert de dienst direct de afgeleide data die nu buiten de selectie valt.
