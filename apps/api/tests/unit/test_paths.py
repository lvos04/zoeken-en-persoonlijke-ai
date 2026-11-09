"""B07-001 — lees-padenallowlist: wat niet geselecteerd is, wordt nooit gelezen (ook niet via symlinks)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from search_ai.search.paths import (
    DEFAULT_EXCLUDES,
    PathGuard,
    PathRuleError,
    PreviewResult,
    iter_candidates,
    matches_exclude,
    normalize_abs,
    preview,
    symlink_in_path,
    validate_patterns,
    validate_read_roots,
)
from tests.conftest import Shares


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("relatief/pad", "pad_niet_absoluut"),
        ("/bronnen/../etc", "pad_met_punten"),
        ("/bronnen/./x", "pad_met_punten"),
        ("/bronnen/a\x00b", "pad_ongeldige_tekens"),
        ("", "pad_leeg"),
        ("/" + "a" * 1100, "pad_te_lang"),
    ],
)
def test_normalize_rejects_unsafe_paths(raw: str, code: str) -> None:
    with pytest.raises(PathRuleError) as exc:
        normalize_abs(raw)
    assert exc.value.code == code


def test_read_roots_never_system_or_own_data() -> None:
    roots, warnings = validate_read_roots(
        ["/", "/etc", "/proc/self", "/appdata", "/appdata/x", "/mnt", "/tmp", "/bronnen", "/bronnen", "rel"],
        Path("/appdata"),
    )
    assert roots == ("/bronnen",)
    assert len(warnings) == 8  # het duplicaat /bronnen wordt stil samengevoegd


def test_source_paths_must_be_inside_read_roots(shares: Shares, tmp_path: Path) -> None:
    roots = (str(shares.root),)
    data = tmp_path / "appdata"
    assert PathGuard.validate_source_paths([str(shares.a)], roots, data) == [str(shares.a)]
    with pytest.raises(PathRuleError) as exc:
        PathGuard.validate_source_paths([str(tmp_path / "elders")], roots, data)
    assert exc.value.code == "pad_buiten_leeswortels"
    with pytest.raises(PathRuleError) as exc:
        PathGuard.validate_source_paths([str(shares.a / ".ssh")], roots, data)
    assert exc.value.code == "pad_uitgesloten"
    with pytest.raises(PathRuleError) as exc:
        PathGuard.validate_source_paths([str(shares.a), str(shares.a / "werk")], roots, data)
    assert exc.value.code == "pad_overlapt"
    with pytest.raises(PathRuleError) as exc:
        PathGuard.validate_source_paths([], roots, data)
    assert exc.value.code == "geen_paden"
    with pytest.raises(PathRuleError) as exc:
        PathGuard.validate_source_paths([str(shares.a)], (), data)
    assert exc.value.code == "geen_leeswortels"


def test_unselected_share_is_never_allowed(shares: Shares) -> None:
    guard = PathGuard((str(shares.root),), (str(shares.a),))
    assert guard.check(str(shares.a / "notities.md")) == (True, "toegestaan")
    assert guard.check(str(shares.b / "dagboek.txt")) == (False, "niet_geselecteerd")
    assert guard.check(str(shares.root)) == (False, "niet_geselecteerd")
    # prefix-truc: /bronnen/documenten-x is niet /bronnen/documenten
    assert guard.check(str(shares.a) + "-x/bestand") == (False, "niet_geselecteerd")
    assert guard.check(str(shares.a / ".ssh" / "id_ed25519")) == (False, "uitgesloten")
    assert guard.check(str(shares.a / ".zfs" / "snapshot" / "auto-1" / "notities.md")) == (False, "uitgesloten")
    assert guard.check(str(shares.a / "server.pem")) == (False, "uitgesloten")


def test_symlink_escape_is_blocked(shares: Shares) -> None:
    guard = PathGuard((str(shares.root),), (str(shares.a),))
    assert guard.check(str(shares.a / "link-naar-b.txt"))[0] is True  # logisch pad lijkt binnen A …
    assert guard.check_real(str(shares.a / "link-naar-b.txt")) == (False, "symlink_buiten_bron")  # … maar is B
    assert guard.check_real(str(shares.a / "map-naar-b" / "dagboek.txt")) == (False, "symlink_buiten_bron")


def test_excludes_case_insensitive_and_custom(shares: Shares) -> None:
    assert matches_exclude(("Foto's", "@eaDir", "x.jpg"), DEFAULT_EXCLUDES) == "@eadir"
    assert matches_exclude((".Trash-1000", "a"), DEFAULT_EXCLUDES) == ".trash*"
    assert matches_exclude(("werk", "offerte.txt"), DEFAULT_EXCLUDES) is None
    assert matches_exclude(("werk", "offerte.txt"), ["werk/*"]) == "werk/*"
    assert matches_exclude(("werk", "sub", "x.txt"), ["werk"]) == "werk"
    with pytest.raises(PathRuleError):
        validate_patterns(["../x"])
    with pytest.raises(PathRuleError):
        validate_patterns([f"p{i}" for i in range(51)])


def test_preview_and_ingest_walk_only_selected_share(shares: Shares) -> None:
    """Acceptatie B07-001 (padenlaag): een niet-geselecteerde share levert nul kandidaten op."""
    guard = PathGuard((str(shares.root),), (str(shares.a),), ("notities.md",))
    result = PreviewResult()
    found = [p for p, _s, _m in iter_candidates(guard, 1000, result)]
    assert found == [str(shares.a / "werk" / "offerte.txt")]
    assert not any(str(shares.b) in p for p in found)
    assert result.skipped_symlink == 2  # link-naar-b.txt en map-naar-b
    assert result.skipped_excluded >= 3  # .ssh, .zfs, server.pem, notities.md

    pv = preview(guard, 1000)
    assert pv.files == 1
    assert pv.sample == ["documenten/werk/offerte.txt"]
    assert pv.paths[0]["bereikbaar"] is True


def test_preview_truncates(shares: Shares) -> None:
    for i in range(5):
        (shares.a / "werk" / f"extra-{i}.txt").write_text("x", encoding="utf-8")
    pv = preview(PathGuard((str(shares.root),), (str(shares.a),)), max_files=3)
    assert pv.files == 3 and pv.truncated is True


def test_missing_mount_is_reported_not_guessed(shares: Shares) -> None:
    missing = shares.root / "nog-niet-gekoppeld"
    pv = preview(PathGuard((str(shares.root),), (str(missing),)), 100)
    assert pv.files == 0 and pv.paths[0]["bereikbaar"] is False and pv.paths[0]["alleen_lezen"] is None


def test_walk_does_not_follow_symlinked_base(shares: Shares) -> None:
    link = shares.root / "alias-van-b"
    os.symlink(shares.b, link)
    pv = preview(PathGuard((str(shares.root),), (str(link),)), 100)
    assert pv.files == 0 and pv.paths[0]["bereikbaar"] is False


def test_selected_path_through_symlinked_ancestor_is_refused_and_never_walked(shares: Shares, tmp_path: Path) -> None:
    """QA P0-afronding (ADR-0010): ``/bronnen/a/map-naar-b/sub`` lijkt binnen share A, maar opent share B."""
    (shares.b / "sub").mkdir()
    (shares.b / "sub" / "geheim.txt").write_text("Privé van B", encoding="utf-8")
    roots = (str(shares.root),)
    via = shares.a / "map-naar-b" / "sub"  # map-naar-b is een symlink naar share B
    assert symlink_in_path(str(via), roots) == str(shares.a / "map-naar-b")
    assert symlink_in_path(str(shares.a / "werk"), roots) is None
    assert symlink_in_path(str(shares.a / "bestaat-nog-niet" / "x"), roots) is None  # niet bestaand ≠ symlink
    assert symlink_in_path(str(tmp_path / "elders"), roots) == str(tmp_path / "elders")  # buiten wortel: fail-closed
    # 1. kiezen in de bronwizard: geweigerd
    with pytest.raises(PathRuleError) as exc:
        PathGuard.validate_source_paths([str(via)], roots, tmp_path / "appdata")
    assert exc.value.code == "pad_via_symlink"
    # 2. ook als zo'n pad toch in een bron staat (bv. de map is na het kiezen door een symlink vervangen):
    #    niets lezen, niets tonen
    guard = PathGuard(roots, (str(via),))
    assert guard.check(str(via / "geheim.txt"))[0] is True  # het logische pad lijkt toegestaan …
    assert guard.check_real(str(via / "geheim.txt")) == (False, "symlink_in_bronpad")  # … maar is het niet
    result = PreviewResult()
    assert list(iter_candidates(guard, 100, result)) == []
    pv = preview(guard, 100)
    assert pv.files == 0 and pv.sample == []
    assert pv.paths[0]["bereikbaar"] is False and pv.paths[0]["via_symlink"] is True
    assert pv.paths[0]["alleen_lezen"] is None
