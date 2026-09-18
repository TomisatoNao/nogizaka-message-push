from argparse import Namespace
from pathlib import Path

import pytest

from tools import deploy_release


ROOT = Path(__file__).resolve().parents[1]


def _args(**overrides):
    values = {
        "host": "nas.example.test",
        "user": "deploy",
        "port": 22,
        "identity": None,
        "remote_dir": "/volume1/docker/nogizaka-push",
        "tag": "sha-1234abc",
        "docker_bin": "/usr/local/bin/docker",
        "compose_file": "docker-compose.yml",
        "health_url": "http://127.0.0.1:46046/api/health/status",
        "attempts": 18,
        "interval": 5,
        "image_repo": "ghcr.io/tomisatonao/nogizaka-message-push",
    }
    values.update(overrides)
    return Namespace(**values)


def test_release_deployer_accepts_immutable_sha_and_version_tags():
    for tag in ("sha-1234abc", "sha-1234567890abcdef", "v1.2.3", "v1.2.3-rc.1"):
        command = deploy_release.build_ssh_command(_args(tag=tag))
        assert tag in command
        assert "BatchMode=yes" in command


def test_release_deployer_rejects_mutable_or_shell_unsafe_tags():
    for tag in ("latest", "main", "sha-123;reboot", "$(whoami)"):
        with pytest.raises(ValueError):
            deploy_release.build_ssh_command(_args(tag=tag))


def test_release_deployer_contains_health_check_and_rollback_without_passwords():
    source = (ROOT / "tools" / "deploy_release.py").read_text(encoding="utf-8")
    assert "rollback" in deploy_release.REMOTE_DEPLOY_SCRIPT
    assert "${DOCKER_BIN}-compose" in deploy_release.REMOTE_DEPLOY_SCRIPT
    assert "nogizaka-message-push:latest" in deploy_release.REMOTE_DEPLOY_SCRIPT
    assert "/api/health/status" in source
    assert "NAS_PASS" not in source
    assert "password=" not in source


def test_compose_files_support_pinned_release_tag():
    for name in ("docker-compose.yml", "docker-compose.with-napcat.yml"):
        compose = (ROOT / name).read_text(encoding="utf-8")
        assert "${APP_IMAGE_TAG:-latest}" in compose


def test_image_publish_is_gated_by_test_job_and_emits_sha_tag():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "needs: test" in workflow
    assert "type=sha,format=short,prefix=sha-" in workflow
    assert "APP_GIT_SHA=${{ github.sha }}" in workflow
    assert not (ROOT / ".github" / "workflows" / "docker-publish.yml").exists()


def test_container_build_exposes_release_metadata_to_health_endpoint():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG APP_VERSION=dev" in dockerfile
    assert "APP_VERSION=${APP_VERSION}" in dockerfile
    assert "APP_GIT_SHA=${APP_GIT_SHA}" in dockerfile
    assert "APP_BUILD_TIME=${APP_BUILD_TIME}" in dockerfile
