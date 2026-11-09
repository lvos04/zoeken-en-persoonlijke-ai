"""Bewaakt de installatieregels (contract §1/§2/§9/§10): standaard Custom App, gepinde images, geen geheimen."""

from __future__ import annotations

import re

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


def test_supervisor_never_touches_sources() -> None:
    sup = (REPO_ROOT / "deploy/allinone/supervisor.py").read_text()
    assert 'MANAGED = ("postgres", "secrets", "backups", "logs")' in sup
    assert "chown(base" not in sup and "chmod(base" not in sup


def test_manifest_is_custom_app_without_compose() -> None:
    m = yaml.safe_load(MANIFEST.read_text())
    assert m["install"]["method"] == "custom_app"
    assert "compose" not in m and "custom_compose_config" not in MANIFEST.read_text()
    assert m["image"]["tag"] != "latest" and m["custom_app"]["ports"][0]["container_port"] == 30450
    assert m["privileges"]["privileged"] is False and m["network"]["host_network"] is False
    assert m["custom_app"]["env"] == []


def test_custom_app_doc_is_form_only() -> None:
    doc = CUSTOM_APP.read_text()
    for field in ("Application Name", "Repository", "Tag", "Container Port", "Host Path", "Mount Path", "Read Only"):
        assert field in doc
    assert "```yaml" not in doc and "custom_compose_config" not in doc
    assert "geen *Install via YAML*" in doc
    assert "NIET GEVALIDEERD" in doc


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
