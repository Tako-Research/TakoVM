"""Tests for docker-compose deployment hardening."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _compose_config() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yaml").read_text(encoding="utf-8"))


def test_api_service_uses_socket_proxy_instead_of_socket_mount():
    """The public API service should not mount the host Docker socket directly."""
    config = _compose_config()
    tako_vm = config["services"]["tako-vm"]
    volumes = tako_vm.get("volumes", [])
    environment = tako_vm.get("environment", [])

    assert not any("/var/run/docker.sock" in volume for volume in volumes)
    assert "DOCKER_HOST=tcp://docker-socket-proxy:2375" in environment
    assert "docker-socket-proxy" in tako_vm["depends_on"]
    assert "docker-internal" in tako_vm["networks"]


def test_socket_proxy_is_internal_and_owns_socket_mount():
    """Only the internal proxy service should receive the Docker socket mount."""
    config = _compose_config()
    proxy = config["services"]["docker-socket-proxy"]
    volumes = proxy.get("volumes", [])
    environment = set(proxy.get("environment", []))

    assert "/var/run/docker.sock:/var/run/docker.sock:ro" in volumes
    assert "docker-internal" in proxy["networks"]
    assert config["networks"]["docker-internal"]["internal"] is True
    assert {"CONTAINERS=1", "IMAGES=1", "INFO=1", "POST=1", "VOLUMES=1"} <= environment
