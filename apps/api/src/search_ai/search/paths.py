"""Lees-padenallowlist (B07-001): welke paden kan deze dienst ooit lezen?

Drie lagen, alle drie fail-closed:
1. **Systeembrede leeswortels** (``SEARCH_AI_READ_ROOTS``, standaard ``/bronnen``). In de Custom App koppelt de gebruiker
   elke share als extra *Host Path* met **Read Only** onder ``/bronnen/<naam>``. Paden daarbuiten bestaan voor deze
   dienst niet. Systeemmappen en de eigen datamap (``/appdata``) kunnen nooit een leeswortel zijn.
2. **Per bron** de ``allowed_paths`` die de beheerder in de bronwizard koos (binnen een leeswortel). Een share die niet
   geselecteerd is, staat in geen enkele bron en wordt dus nooit gelezen.
3. **Uitsluitingen**: een vaste lijst gevoelige patronen (sleutels, ``.ssh``, ``.zfs``-snapshots, prullenbakken, …) die
   altijd geldt, plus eigen patronen per bron.

Bij elk bestand wordt bovendien het echte pad (symlinks opgelost) opnieuw gecontroleerd: een symlink in share A die
naar share B wijst, wordt overgeslagen. De voorvertoning leest alleen namen/grootte/datum, nooit inhoud.
"""

from __future__ import annotations

import fnmatch
import os
import posixpath
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# Mappen die nooit een leeswortel of bronpad mogen zijn of bevatten (container-systeempaden).
SYSTEM_DIRS = (
    "/proc",
    "/sys",
    "/dev",
    "/etc",
    "/root",
    "/run",
    "/var/run",
    "/var/lib",
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/boot",
    "/opt",
)

# Altijd uitgesloten (hoofdletterongevoelig, per padonderdeel). Bewust ruim: liever iets te veel overslaan.
DEFAULT_EXCLUDES = (
    ".zfs",
    ".snapshot",
    ".snapshots",
    "@eadir",
    "#recycle",
    ".trash*",
    "$recycle.bin",
    "lost+found",
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".kube",
    ".docker",
    ".password-store",
    ".git",
    "secrets",
    ".env",
    ".env.*",
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
    "*.kdbx",
    "*.ovpn",
    "id_rsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "*.gpg",
)

MAX_PATH_LEN = 1024
MAX_PATTERNS = 50
MAX_PATTERN_LEN = 200


class PathRuleError(ValueError):
    """Ongeldig pad of patroon; ``code`` is machineleesbaar, de boodschap is Nederlands."""

    def __init__(self, code: str, message: str, path: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


def _is_within(child: str, parent: str) -> bool:
    if parent == "/":
        return child.startswith("/")
    return child == parent or child.startswith(parent.rstrip("/") + "/")


def normalize_abs(raw: str, label: str = "Pad") -> str:
    if not isinstance(raw, str) or not raw:
        raise PathRuleError("pad_leeg", f"{label} is leeg.")
    if len(raw) > MAX_PATH_LEN:
        raise PathRuleError("pad_te_lang", f"{label} is te lang (max. {MAX_PATH_LEN} tekens).", raw[:80])
    if "\x00" in raw or any(ord(c) < 32 for c in raw):
        raise PathRuleError("pad_ongeldige_tekens", f"{label} bevat ongeldige tekens.", raw)
    if not raw.startswith("/"):
        raise PathRuleError("pad_niet_absoluut", f"{label} moet een absoluut pad zijn (beginnend met /).", raw)
    parts = raw.split("/")
    if any(p in ("..", ".") for p in parts):
        raise PathRuleError("pad_met_punten", f"{label} mag geen '.' of '..' bevatten.", raw)
    norm = posixpath.normpath(raw)
    if norm.startswith("//"):
        norm = "/" + norm.lstrip("/")
    return norm


def validate_read_roots(roots: Iterable[str], data_dir: Path) -> tuple[tuple[str, ...], list[str]]:
    """Geeft (geldige wortels, waarschuwingen). Ongeldige wortels worden genegeerd (fail-closed), nooit verruimd."""
    valid: list[str] = []
    warnings: list[str] = []
    data = posixpath.normpath(str(data_dir))
    for raw in roots:
        try:
            root = normalize_abs(raw, "Leeswortel")
        except PathRuleError as exc:
            warnings.append(f"leeswortel {raw!r} genegeerd: {exc}")
            continue
        if root == "/" or root in ("/mnt", "/home", "/var", "/srv", "/tmp"):  # noqa: S108 - weigeren, niet gebruiken
            warnings.append(f"leeswortel {root!r} genegeerd: te ruim (kies een specifieke map, bv. /bronnen)")
            continue
        if any(_is_within(root, s) or _is_within(s, root) for s in SYSTEM_DIRS):
            warnings.append(f"leeswortel {root!r} genegeerd: systeemmap")
            continue
        if _is_within(root, data) or _is_within(data, root):
            warnings.append(f"leeswortel {root!r} genegeerd: overlapt met de eigen datamap {data}")
            continue
        if root not in valid:
            valid.append(root)
    return tuple(valid), warnings


def matches_exclude(rel_parts: tuple[str, ...], patterns: Iterable[str]) -> str | None:
    """Geeft het eerste patroon dat het relatieve pad uitsluit, of None."""
    lowered = tuple(p.lower() for p in rel_parts)
    rel = "/".join(lowered)
    for pat in patterns:
        p = pat.lower().strip()
        if not p:
            continue
        if "/" in p:
            if fnmatch.fnmatchcase(rel, p.strip("/")) or fnmatch.fnmatchcase(rel, p.strip("/") + "/*"):
                return pat
        elif any(fnmatch.fnmatchcase(part, p) for part in lowered):
            return pat
    return None


def validate_patterns(patterns: Iterable[str]) -> list[str]:
    out: list[str] = []
    for raw in patterns:
        if not isinstance(raw, str):
            raise PathRuleError("patroon_ongeldig", "Een uitsluitpatroon moet tekst zijn.")
        p = raw.strip()
        if not p:
            continue
        if len(p) > MAX_PATTERN_LEN or "\x00" in p or ".." in p.split("/"):
            raise PathRuleError("patroon_ongeldig", f"Uitsluitpatroon {p[:40]!r} is ongeldig.")
        if p not in out:
            out.append(p)
    if len(out) > MAX_PATTERNS:
        raise PathRuleError("te_veel_patronen", f"Maximaal {MAX_PATTERNS} uitsluitpatronen per bron.")
    return out


@dataclass(frozen=True)
class PathGuard:
    """Bewaker voor één bron: leeswortels × ``allowed_paths`` × uitsluitingen."""

    read_roots: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    exclude_patterns: tuple[str, ...] = field(default_factory=tuple)

    @property
    def all_excludes(self) -> tuple[str, ...]:
        return DEFAULT_EXCLUDES + self.exclude_patterns

    @staticmethod
    def validate_source_paths(paths: Iterable[str], read_roots: tuple[str, ...], data_dir: Path) -> list[str]:
        """Controleert de door de beheerder gekozen paden. Geen wortels = niets toegestaan."""
        if not read_roots:
            raise PathRuleError(
                "geen_leeswortels",
                "Er zijn geen geldige leeswortels ingesteld; koppel shares als Read Only onder /bronnen.",
            )
        out: list[str] = []
        data = posixpath.normpath(str(data_dir))
        for raw in paths:
            p = normalize_abs(raw)
            root = next((r for r in read_roots if _is_within(p, r)), None)
            if root is None:
                raise PathRuleError(
                    "pad_buiten_leeswortels",
                    f"Pad {p} ligt buiten de toegestane leeswortels ({', '.join(read_roots)}).",
                    p,
                )
            if _is_within(p, data) or _is_within(data, p):
                raise PathRuleError("pad_in_datamap", "De eigen datamap van de app kan geen bron zijn.", p)
            rel = tuple(x for x in p[len(root) :].split("/") if x)
            hit = matches_exclude(rel, DEFAULT_EXCLUDES)
            if hit is not None:
                raise PathRuleError("pad_uitgesloten", f"Pad {p} valt onder een vaste uitsluiting ({hit}).", p)
            if any(_is_within(p, q) or _is_within(q, p) for q in out):
                raise PathRuleError("pad_overlapt", f"Pad {p} overlapt met een ander gekozen pad.", p)
            out.append(p)
        if not out:
            raise PathRuleError("geen_paden", "Kies minstens één map voor deze bron.")
        if len(out) > 50:
            raise PathRuleError("te_veel_paden", "Maximaal 50 mappen per bron.")
        return out

    def _allowed_root_for(self, path: str) -> str | None:
        if not any(_is_within(path, r) for r in self.read_roots):
            return None
        return next((a for a in self.allowed_paths if _is_within(path, a)), None)

    def check(self, path: str) -> tuple[bool, str]:
        """Is dit (absolute, logische) pad leesbaar voor deze bron? Geeft (ok, reden)."""
        try:
            p = normalize_abs(path)
        except PathRuleError as exc:
            return False, exc.code
        base = self._allowed_root_for(p)
        if base is None:
            return False, "niet_geselecteerd"
        rel = tuple(x for x in p[len(base) :].split("/") if x)
        if matches_exclude(rel, self.all_excludes) is not None:
            return False, "uitgesloten"
        return True, "toegestaan"

    def check_real(self, path: str) -> tuple[bool, str]:
        """Zoals ``check``, maar ook op het echte pad (symlinks opgelost) binnen het echte toegestane pad."""
        ok, reason = self.check(path)
        if not ok:
            return ok, reason
        p = normalize_abs(path)
        base = self._allowed_root_for(p)
        if base is None:  # pragma: no cover - check() hierboven garandeert een basis
            return False, "niet_geselecteerd"
        real = os.path.realpath(p)
        real_base = os.path.realpath(base)
        if not _is_within(real, real_base):
            return False, "symlink_buiten_bron"
        return True, "toegestaan"


def is_readonly_mount(path: str) -> bool | None:
    """True/False als het pad bestaat; None als het (nog) niet bestaat of niet te bepalen is."""
    try:
        return bool(os.statvfs(path).f_flag & os.ST_RDONLY)
    except OSError:
        return None


@dataclass
class PreviewResult:
    files: int = 0
    bytes: int = 0
    skipped_excluded: int = 0
    skipped_symlink: int = 0
    skipped_special: int = 0
    unreadable: int = 0
    truncated: bool = False
    sample: list[str] = field(default_factory=list)
    paths: list[dict[str, object]] = field(default_factory=list)


def iter_candidates(guard: PathGuard, max_files: int, result: PreviewResult) -> Iterator[tuple[str, int, datetime]]:
    """Loopt de toegestane mappen af zonder symlinks te volgen en zonder bestanden te openen."""
    for base in guard.allowed_paths:
        if not os.path.isdir(base) or os.path.islink(base):
            continue

        def _onerror(_err: OSError) -> None:
            result.unreadable += 1

        for dirpath, dirnames, filenames in os.walk(base, topdown=True, onerror=_onerror, followlinks=False):
            rel_dir = tuple(x for x in dirpath[len(base) :].split("/") if x)
            keep: list[str] = []
            for d in dirnames:
                full = os.path.join(dirpath, d)
                if os.path.islink(full):
                    result.skipped_symlink += 1
                elif matches_exclude((*rel_dir, d), guard.all_excludes) is not None:
                    result.skipped_excluded += 1
                else:
                    keep.append(d)
            dirnames[:] = sorted(keep)
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                if matches_exclude((*rel_dir, name), guard.all_excludes) is not None:
                    result.skipped_excluded += 1
                    continue
                try:
                    st = os.lstat(full)
                except OSError:
                    result.unreadable += 1
                    continue
                if os.path.islink(full):
                    result.skipped_symlink += 1
                    continue
                if not os.path.isfile(full):
                    result.skipped_special += 1
                    continue
                ok, _ = guard.check_real(full)
                if not ok:
                    result.skipped_symlink += 1
                    continue
                if result.files >= max_files:
                    result.truncated = True
                    return
                result.files += 1
                result.bytes += st.st_size
                yield full, st.st_size, datetime.fromtimestamp(st.st_mtime, UTC)


def preview(guard: PathGuard, max_files: int, sample_size: int = 20) -> PreviewResult:
    """Bronwizard-voorvertoning: tellingen en een kleine steekproef van relatieve namen. Leest geen inhoud."""
    result = PreviewResult()
    for base in guard.allowed_paths:
        exists = os.path.isdir(base) and not os.path.islink(base)
        result.paths.append(
            {"pad": base, "bereikbaar": exists, "alleen_lezen": is_readonly_mount(base) if exists else None}
        )
    for full, _size, _mtime in iter_candidates(guard, max_files, result):
        if len(result.sample) < sample_size:
            base = next(a for a in guard.allowed_paths if _is_within(full, a))
            result.sample.append(posixpath.basename(base) + "/" + full[len(base) :].lstrip("/"))
    return result
