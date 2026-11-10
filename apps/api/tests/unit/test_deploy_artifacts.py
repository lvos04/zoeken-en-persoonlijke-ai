"""Bewaakt de installatieregels (contract 1.2 §1/§2/§9/§10): standaard Custom App, gepinde images, geen geheimen, geen
root-proces na de eigenaarschapsstap (ook PID 1 niet)."""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest
import yaml

from tests.conftest import REPO_ROOT

DOCKERFILE = REPO_ROOT / "deploy/allinone/Dockerfile"
MANIFEST = REPO_ROOT / "deploy/manifest/zoeken-en-persoonlijke-ai.manifest.yaml"
CUSTOM_APP = REPO_ROOT / "deploy/truenas/CUSTOM_APP.md"
COMPOSE = REPO_ROOT / "deploy/dev/compose.yaml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
WORKFLOWS = REPO_ROOT / ".github/workflows"


def test_base_images_pinned_by_digest_and_never_latest() -> None:
    text = DOCKERFILE.read_text()
    images = re.findall(r"^ARG \w+_IMAGE=(\S+)$", text, flags=re.M)
    assert len(images) == 2
    for img in images:
        assert "@sha256:" in img and ":latest" not in img, img
    assert "pgvector:0.8.7-pg16" in text
    assert "docker.sock" not in text
    # sandbox-CA nooit hard in de Dockerfile: alleen als optionele BuildKit-secret
    assert "ca-bundle.crt" not in text and "id=ca_bundle,required=false" in text
    assert not re.search(r"chown[^;\n]*/bronnen", text)  # /bronnen wordt nooit van eigenaar gewisseld


def test_image_ships_the_pinned_peil_copy() -> None:
    """De statuspagina in de image gebruikt de versie-gepinde Peil-kopie (O-14), niet een eigen variant."""
    text = DOCKERFILE.read_text()
    assert "COPY design/ontwerptaal/statuspagina.html /usr/local/lib/search-ai/peil/statuspagina.html" in text
    assert "SEARCH_AI_PEIL_DIR=/usr/local/lib/search-ai/peil" in text
    assert "load_template(Path('/usr/local/lib/search-ai/peil'))" in text  # bouw faalt bij een gewijzigde kopie
    ignored = (REPO_ROOT / ".dockerignore").read_text().split()
    assert "design" not in ignored and "design/ontwerptaal" not in ignored
    smoke = (REPO_ROOT / "deploy/allinone/smoke-test.sh").read_text()
    assert "Peil statuspagina.css 1.1.1" in smoke


def test_supervisor_never_touches_sources() -> None:
    sup = (REPO_ROOT / "deploy/allinone/supervisor.py").read_text()
    assert 'MANAGED = ("postgres", "secrets", "backups", "logs")' in sup
    assert "chown(base" not in sup and "chmod(base" not in sup


def test_manifest_is_custom_app_without_compose() -> None:
    m = yaml.safe_load(MANIFEST.read_text())
    assert m["install"]["method"] == "custom_app"
    assert "compose" not in m and "custom_compose_config" not in MANIFEST.read_text()
    assert m["image"]["tag"] != "latest" and m["network"]["ports"][0]["container_port"] == 30450
    assert m["privileges"]["privileged"] is False and m["network"]["host_network"] is False
    assert m["custom_app"]["env"] == []


def test_manifest_follows_contract_1_2_section_9() -> None:
    m = yaml.safe_load(MANIFEST.read_text())
    assert "platformcontract 1.2" in MANIFEST.read_text()
    assert "ports" not in m["custom_app"]  # vervallen: network.ports is de enige bron
    assert "joins_platform_network" not in m["network"]  # vervallen: apps delen nooit een Docker-netwerk
    names = {p["name"] for p in m["network"]["ports"]}
    assert m["custom_app"]["portal"]["port"] in names
    assert m["custom_app"]["gpu"] == "none" and m["privileges"]["gpu"] == "none"
    assert m["app_id"] == "zoeken-en-persoonlijke-ai"


def test_custom_app_doc_is_form_only() -> None:
    doc = CUSTOM_APP.read_text()
    for field in ("Application Name", "Repository", "Tag", "Container Port", "Host Path", "Mount Path", "Read Only"):
        assert field in doc
    assert "```yaml" not in doc and "custom_compose_config" not in doc
    assert "geen *Install via YAML*" in doc
    assert "NIET GEVALIDEERD" in doc


def test_custom_app_doc_contract_1_2() -> None:
    doc = CUSTOM_APP.read_text()
    assert "@sha256:" in doc and "release/digests.json" in doc  # tag met digest (§2.2)
    assert "HUB_KEY_FINGERPRINT" in doc and "HUB_ENROLLMENT_TOKEN" in doc and "HUB_URL" in doc
    assert "terugval" in doc and "registry" in doc.lower()  # publieke image, registry-login alleen als terugval (§2.1)
    assert "alleen via de Hub" in doc and "ontkoppelcode uit de app-log" not in doc  # §3.7
    env = ENV_EXAMPLE.read_text()
    assert "HUB_KEY_FINGERPRINT=" in env and "JWKS_TTL" not in env and "JWKS_MAX_STALE" not in env


def test_compose_is_dev_only() -> None:
    text = COMPOSE.read_text()
    assert "GEEN installatiemethode" in text
    assert ":ro" in text and "docker.sock" not in text


def test_env_example_has_no_secret_values() -> None:
    for line in ENV_EXAMPLE.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if any(s in key for s in ("TOKEN", "SECRET", "PASSWORD", "KEY")):
            assert value == "", f"{key} heeft een waarde in .env.example"


def test_workflow_actions_pinned_by_sha() -> None:
    for wf in WORKFLOWS.glob("*.yml"):
        for ref in re.findall(r"uses:\s*(\S+)", wf.read_text()):
            assert re.search(r"@[0-9a-f]{40}$", ref), f"{wf.name}: {ref} niet op SHA gepind"
        assert ":latest" not in wf.read_text()


def _load_supervisor() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("sai_supervisor", REPO_ROOT / "deploy/allinone/supervisor.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    ("uid", "gid", "allowed"), [(0, 0, True), (568, 568, True), (1000, 1000, False), (568, 0, False)]
)
def test_supervisor_refuses_other_run_user_with_clear_dutch_error(
    monkeypatch: pytest.MonkeyPatch, uid: int, gid: int, allowed: bool
) -> None:
    sup = _load_supervisor()
    errors: list[str] = []

    def fake_fail(msg: str, code: int = 78) -> None:
        errors.append(msg)
        raise SystemExit(code)

    monkeypatch.setattr(sup, "fail", fake_fail)
    monkeypatch.setattr(sup.os, "getuid", lambda: uid)
    monkeypatch.setattr(sup.os, "getgid", lambda: gid)
    if allowed:
        sup.check_run_user()
        assert errors == []
    else:
        with pytest.raises(SystemExit):
            sup.check_run_user()
        assert f"de app draait als gebruiker {uid}:{gid}" in errors[0] and "Custom User" in errors[0]
    main_src = (REPO_ROOT / "deploy/allinone/supervisor.py").read_text()
    assert main_src.index("check_run_user()\n    prepare_dirs()") > 0  # vóór de mappen en vóór initdb


def test_smoke_test_inspects_secrets_via_container_not_as_host_user() -> None:
    # /appdata/secrets is 0700/568: de niet-root host-gebruiker (bv. de GitHub-runner) mag er terecht niet in
    smoke = (REPO_ROOT / "deploy/allinone/smoke-test.sh").read_text()
    assert "$WORK/root/secrets" not in smoke
    assert 'on_disk() { docker run --rm --network none -v "$WORK:/w:ro"' in smoke


def test_supervisor_sets_no_new_privs_for_itself_and_children() -> None:
    """Contract 1.2 §2.2: het TrueNAS Custom App-formulier zet geen no-new-privileges, dus de image zelf (ADR-0010)."""
    sup = _load_supervisor()
    out = subprocess.run(
        [sys.executable, "-c", "print(open('/proc/self/status').read())"],
        preexec_fn=sup.demote,  # precies zoals de bewaker zijn kinderen start
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert re.search(r"^NoNewPrivs:\s+1$", out, flags=re.M), out
    main_src = (REPO_ROOT / "deploy/allinone/supervisor.py").read_text()
    assert "demote()  # vanaf hier draait niets meer als root" in main_src
    assert main_src.index("if not no_new_privs_active():") > main_src.index("demote()  # vanaf hier")
    smoke = (REPO_ROOT / "deploy/allinone/smoke-test.sh").read_text()
    run_fn = smoke[smoke.index("run() {") : smoke.index("}", smoke.index("run() {"))]
    body = run_fn.split("\n", 1)[1]
    assert "no-new-privileges" not in body and "cap-drop" not in body  # zoals het formulier
    assert "--security-opt" not in smoke.split("== 4.")[1].split("== 5.")[0]  # ook 'Custom User 568' zonder vlag


def test_no_root_init_supervisor_is_pid_1() -> None:
    """Contract 1.2 §2.2/R11: geen dumb-init/tini die als root PID 1 blijft; de bewaker is PID 1 en wisselt zelf."""
    text = DOCKERFILE.read_text()
    assert "dumb-init" not in text.replace("Geen ENTRYPOINT-init", "") and "tini" not in text
    assert not re.search(r"^ENTRYPOINT", text, flags=re.M)
    assert 'CMD ["python", "/usr/local/lib/search-ai/supervisor.py"]' in text
    sup = (REPO_ROOT / "deploy/allinone/supervisor.py").read_text()
    main = sup[sup.index("def main() -> None:") :]
    # PID 1 vangt SIGTERM zelf af, en wel vóór de (soms lange) eigenaarschapsstap
    assert main.index("signal.signal(signal.SIGTERM, shutdown)") < main.index("prepare_dirs()")
    assert "reap_orphans(" in main
    smoke = (REPO_ROOT / "deploy/allinone/smoke-test.sh").read_text()
    for needle in (
        "privs_all_568",
        "pid1_is_supervisor",
        "no_zombies_after_orphan",
        'f.get("CapEff") == leeg and f.get("CapPrm") == leeg',
        'f.get("CapAmb") == leeg',
        '(not root_start or f.get("CapBnd") == leeg)',
        "healthcheck_drops_privs $NAME root",
        "healthcheck_drops_privs $NAME-568 custom-user",
    ):
        assert needle in smoke
    assert "! log_has $NAME 'ontkoppelcode'" in smoke


def test_reap_orphans_only_reaps_unknown_zombies() -> None:
    sup = _load_supervisor()
    known, orphan = os.fork(), None
    if known == 0:
        os._exit(0)
    orphan = os.fork()
    if orphan == 0:
        os._exit(0)
    deadline = time.monotonic() + 5
    reaped = 0
    while time.monotonic() < deadline and reaped == 0:
        reaped = sup.reap_orphans({known})
        time.sleep(0.05)
    assert reaped == 1
    try:  # het onbekende kind is opgeruimd …
        os.waitpid(orphan, os.WNOHANG)
        raise AssertionError("verweesd kind niet opgeruimd")
    except ChildProcessError:
        pass
    assert os.waitpid(known, 0)[0] == known  # … het bekende kind niet (zijn exitstatus blijft voor de bewaker)


def test_healthcheck_drops_root_first() -> None:
    """Contract 1.3 §2.2: exec-vorm; als eerste handeling (bij root) bounding set legen, dan gid/uid, dan no_new_privs;
    pas daarna iets controleren. Met --controle meldt hij de rechten na de wissel (rooktest, apart)."""
    hc = (REPO_ROOT / "deploy/allinone/healthcheck.py").read_text()
    code = hc[hc.index("def drop_privileges()") :]
    assert code.index("libc.prctl(PR_CAPBSET_DROP, cap") < code.index("os.setgroups([])") < code.index("os.setuid(UID)")
    assert code.index("os.setuid(UID)") < code.index("libc.prctl(PR_SET_NO_NEW_PRIVS, 1") < code.index("urlopen(")
    assert '"--controle" in sys.argv' in hc and "sys.exit(1)  # nooit als root doorgaan" in hc
    assert 'HEALTHCHECK --interval=30s --timeout=8s --start-period=90s --retries=3 \\\n  CMD ["python",' in (
        DOCKERFILE.read_text()
    )


def test_healthcheck_controlevlag_faalt_buiten_568() -> None:
    """Negatief: buiten de container (geen 568) meldt de controlevlag FOUT en exit 1; nooit een HTTP-verzoek."""
    out = subprocess.run(
        [sys.executable, str(REPO_ROOT / "deploy/allinone/healthcheck.py"), "--controle"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "SEARCH_AI_UID": "568", "SEARCH_AI_GID": "568"},
    )
    if os.getuid() == 568:  # pragma: no cover - alleen in een container als 568
        pytest.skip("draait al als 568")
    assert out.returncode == 1 and "controle: FOUT" in out.stdout and "NoNewPrivs: 1" in out.stdout


def test_supervisor_leegt_de_bounding_set_voor_de_wissel(monkeypatch: pytest.MonkeyPatch) -> None:
    """K3: bij een start als root eerst PR_CAPBSET_DROP voor elke capability; een capability die de kernel niet kent
    (EINVAL) wordt overgeslagen, elke andere fout stopt de start (nooit doorstarten met een volle bounding set)."""
    import ctypes
    import errno

    sup = _load_supervisor()
    src = (REPO_ROOT / "deploy/allinone/supervisor.py").read_text()
    demote = src[src.index("def demote() -> None:") :]
    assert demote.index("drop_bounding_set()") < demote.index("os.setgroups([])") < demote.index("os.setuid(UID)")
    calls: list[int] = []

    class FakeLibc:
        def __init__(self, fout: int | None) -> None:
            self.fout = fout

        def prctl(self, op: int, cap: int, *_a: int) -> int:
            assert op == sup.PR_CAPBSET_DROP
            calls.append(cap)
            if cap == 40:
                ctypes.set_errno(errno.EINVAL)
                return -1
            if self.fout is not None and cap == 3:
                ctypes.set_errno(self.fout)
                return -1
            return 0

    monkeypatch.setattr(sup, "_cap_last", lambda: 40)
    monkeypatch.setattr(sup, "_status_field", lambda name: "0000000000000000")
    monkeypatch.setattr(sup.ctypes, "CDLL", lambda *_a, **_k: FakeLibc(None))
    sup.drop_bounding_set()
    assert calls == list(range(41))
    monkeypatch.setattr(sup.ctypes, "CDLL", lambda *_a, **_k: FakeLibc(errno.EPERM))
    with pytest.raises(RuntimeError, match="PR_CAPBSET_DROP 3"):
        sup.drop_bounding_set()
    monkeypatch.setattr(sup.ctypes, "CDLL", lambda *_a, **_k: FakeLibc(None))
    monkeypatch.setattr(sup, "_status_field", lambda name: "000001ffffffffff")
    with pytest.raises(RuntimeError, match="bounding set"):
        sup.drop_bounding_set()
    main = src[src.index("def main() -> None:") :]
    assert "except (RuntimeError, OSError) as exc:" in main and "rechten niet afgeven" in main


@pytest.mark.parametrize("sub", ["postgres", "secrets", "backups", "logs"])
def test_supervisor_refuses_symlinked_own_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sub: str) -> None:
    """QA contract 1.2 §2.2: als root nooit een link in /appdata volgen; anders kan een 568-proces root bij de volgende
    start laten chownen buiten /appdata (bv. in /usr/local) en zo weer root worden."""
    sup = _load_supervisor()
    errors: list[str] = []

    def fake_fail(msg: str, code: int = 78) -> None:
        errors.append(msg)
        raise SystemExit(code)

    elders = tmp_path / "elders"
    elders.mkdir()
    (elders / "bestand").write_text("niet aanraken", encoding="utf-8")
    data = tmp_path / "appdata"
    data.mkdir()
    monkeypatch.setattr(sup, "fail", fake_fail)
    monkeypatch.setattr(sup, "DATA", data)
    sup.refuse_symlinks()  # zonder links: niets aan de hand
    assert errors == []
    (data / sub).symlink_to(elders, target_is_directory=True)
    with pytest.raises(SystemExit):
        sup.refuse_symlinks()
    assert f"{data / sub} is een symbolische koppeling" in errors[0]
    main_src = (REPO_ROOT / "deploy/allinone/supervisor.py").read_text()
    prep = main_src[main_src.index("def prepare_dirs() -> None:") :]
    assert prep.index("refuse_symlinks()") < prep.index("os.chown(DATA")  # vóór elke eigenaarschapsstap


def test_supervisor_socket_dir_link_is_replaced_never_followed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sup = _load_supervisor()
    target = tmp_path / "usr-local-lib"
    target.mkdir()
    sock = tmp_path / "search-ai-pg"
    sock.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(sup, "SOCKET_DIR", sock)
    chowned: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        sup.os, "chown", lambda p, u, g, follow_symlinks=True: chowned.append((str(p), follow_symlinks))
    )
    sup.prepare_socket_dir(root=True)
    assert sock.is_dir() and not sock.is_symlink() and target.is_dir()
    assert chowned == [(str(sock), False)]
    sock.rmdir()
    sock.write_text("geen map", encoding="utf-8")
    sup.prepare_socket_dir(root=False)
    assert sock.is_dir() and not sock.is_symlink()
