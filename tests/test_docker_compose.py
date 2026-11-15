from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _compose_config() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yaml").read_text(encoding="utf-8"))


def test_api_service_uses_socket_proxy_instead_of_socket_mount():
    config = _compose_config()
    tako_vm = config["services"]["tako-vm"]
    volumes = tako_vm.get("volumes", [])
    environment = tako_vm.get("environment", [])

    assert not any("/var/run/docker.sock" in volume for volume in volumes)
    assert "DOCKER_HOST=tcp://docker-socket-proxy:2375" in environment
    assert "docker-socket-proxy" in tako_vm["depends_on"]
    assert "docker-internal" in tako_vm["networks"]


def test_socket_proxy_is_internal_and_owns_socket_mount():
    config = _compose_config()
    proxy = config["services"]["docker-socket-proxy"]
    volumes = proxy.get("volumes", [])
    environment = set(proxy.get("environment", []))

    assert "/var/run/docker.sock:/var/run/docker.sock:ro" in volumes
    assert "docker-internal" in proxy["networks"]
    assert config["networks"]["docker-internal"]["internal"] is True
    assert {"CONTAINERS=1", "IMAGES=1", "INFO=1", "POST=1", "VOLUMES=1"} <= environment


def _published_ports(service: dict) -> list[str]:
    return [str(mapping) for mapping in service.get("ports", [])]


def test_no_service_publishes_postgres_to_the_host():
    config = _compose_config()

    for name, service in config["services"].items():
        for mapping in _published_ports(service):
            host_port = mapping.split(":")[-2] if mapping.count(":") >= 1 else mapping
            assert host_port != "5432", f"service '{name}' publishes Postgres: {mapping}"
            assert not mapping.endswith(":5432"), f"service '{name}' publishes Postgres: {mapping}"


def test_postgres_credentials_are_not_hardcoded_defaults():
    config = _compose_config()
    environment = config["services"]["postgres"].get("environment", [])
    settings = dict(entry.split("=", 1) for entry in environment)

    assert "POSTGRES_PASSWORD" in settings
    assert settings["POSTGRES_PASSWORD"] != "postgres"
    assert settings["POSTGRES_USER"] != "postgres"
    assert settings["POSTGRES_PASSWORD"].startswith("${")
    assert settings["POSTGRES_USER"].startswith("${")


def test_api_port_is_published_on_loopback_only():
    config = _compose_config()
    tako_vm = config["services"]["tako-vm"]
    published = _published_ports(tako_vm)

    assert published == ["127.0.0.1:8000:8000"]

    environment = tako_vm.get("environment", [])
    assert "TAKO_VM_ALLOW_UNAUTHENTICATED_NETWORK_ACCESS=true" in environment
