"""
Pytest configuration for Tako VM tests.
"""

import inspect
import os
import subprocess
import tempfile
import uuid
import warnings
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql

# Suppress urllib3 LibreSSL warning on macOS (harmless)
warnings.filterwarnings("ignore", category=DeprecationWarning, module="urllib3")
try:
    from urllib3.exceptions import NotOpenSSLWarning

    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)
except ImportError:
    pass


def is_docker_available() -> bool:
    """Check if Docker is available and running."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def is_executor_image_available() -> bool:
    """Check if the executor Docker image exists."""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "code-executor:latest"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def is_running_in_vm() -> bool:
    """
    Detect if tests are running in an environment where host mounts don't work.

    This includes VM environments (e.g., Lima on macOS) and CI environments
    where temp directory mounts may not work correctly with Docker.
    """
    # Check for CI environment (GitHub Actions, GitLab CI, etc.)
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        return True

    # Check for Lima environment
    if os.path.exists("/Users") and os.path.exists("/home"):
        # We're in Lima (macOS paths mounted + Linux home exists)
        return True

    # Check if temp dir is accessible from Docker
    # This catches cases where Docker is remote or in a VM
    try:
        temp_dir = tempfile.gettempdir()
        result = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{temp_dir}:/test:ro", "alpine", "ls", "/test"],
            capture_output=True,
            timeout=30,
            check=False,
        )
        # If mount fails or is empty when it shouldn't be, we're in a VM
        if result.returncode != 0:
            return True
    except Exception:
        pass

    return False


def is_gvisor_available() -> bool:
    """Check if gVisor runtime is available."""
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.Runtimes}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0 and "runsc" in result.stdout
    except Exception:
        return False


# Check Docker availability once at module load
DOCKER_AVAILABLE = is_docker_available()
EXECUTOR_IMAGE_AVAILABLE = is_executor_image_available()
RUNNING_IN_VM = is_running_in_vm() if DOCKER_AVAILABLE else False
GVISOR_AVAILABLE = is_gvisor_available() if DOCKER_AVAILABLE else False


# Skip markers for tests that require Docker
requires_docker = pytest.mark.skipif(not DOCKER_AVAILABLE, reason="Docker is not available")

requires_executor_image = pytest.mark.skipif(
    not EXECUTOR_IMAGE_AVAILABLE, reason="Docker executor image (code-executor:latest) not built"
)


def pytest_configure(config):
    """Add custom markers."""
    config.addinivalue_line("markers", "requires_docker: mark test as requiring Docker")
    config.addinivalue_line(
        "markers", "requires_executor_image: mark test as requiring the executor image"
    )
    config.addinivalue_line(
        "markers", "requires_host_mounts: mark test as requiring host path mounts (skip in VM)"
    )
    config.addinivalue_line("markers", "requires_gvisor: mark test as requiring gVisor runtime")


def pytest_collection_modifyitems(config, items):
    """Auto-apply skip markers based on Docker availability."""
    for item in items:
        # Auto-skip sandbox tests if Docker/executor image not available
        if "test_sandbox" in item.nodeid:
            if not DOCKER_AVAILABLE:
                item.add_marker(pytest.mark.skip(reason="Docker not available"))
            elif not EXECUTOR_IMAGE_AVAILABLE:
                item.add_marker(
                    pytest.mark.skip(
                        reason="Executor image not built. Run: docker build -t code-executor:latest -f docker/Dockerfile.executor ."
                    )
                )

        # Auto-skip API tests if Docker not available
        if "test_api" in item.nodeid and "TestHealthEndpoint" not in item.nodeid:
            if not DOCKER_AVAILABLE:
                item.add_marker(pytest.mark.skip(reason="Docker not available"))

        # Auto-skip proc exposure tests if Docker/executor image not available
        if "test_proc_exposure" in item.nodeid:
            if not DOCKER_AVAILABLE:
                item.add_marker(pytest.mark.skip(reason="Docker not available"))
            elif not EXECUTOR_IMAGE_AVAILABLE:
                item.add_marker(
                    pytest.mark.skip(
                        reason="Executor image not built. Run: docker build -t code-executor:latest -f docker/Dockerfile.executor ."
                    )
                )

        # Skip tests that require host mounts when running in a VM
        if item.get_closest_marker("requires_host_mounts"):
            if RUNNING_IN_VM:
                item.add_marker(
                    pytest.mark.skip(
                        reason="Test requires host path mounts which don't work in VM environments"
                    )
                )

        # Skip tests that require gVisor when it's not available
        if item.get_closest_marker("requires_gvisor"):
            if not GVISOR_AVAILABLE:
                item.add_marker(pytest.mark.skip(reason="gVisor runtime not available"))


@pytest.fixture(scope="session")
def docker_available():
    """Fixture to check if Docker is available."""
    return DOCKER_AVAILABLE


@pytest.fixture(scope="session")
def executor_image_available():
    """Fixture to check if executor image is available."""
    return EXECUTOR_IMAGE_AVAILABLE


@pytest.fixture(scope="session")
def gvisor_available():
    """Fixture to check if gVisor is available."""
    return GVISOR_AVAILABLE


@pytest.fixture(scope="session")
def running_in_vm():
    """Fixture to check if running in a VM environment."""
    return RUNNING_IN_VM


@pytest.fixture
def temp_data_dir():
    """
    Create a temporary data directory for isolated tests.

    Resets config and sets environment variables to use a temp directory,
    ensuring test isolation from user data.
    """
    from tako_vm.config import reset_config

    with tempfile.TemporaryDirectory() as tmpdir:
        original_data_dir = os.environ.get("TAKO_VM_DATA_DIR")
        original_db_url = os.environ.get("TAKO_VM_DATABASE_URL")
        schema_created = False

        raw_db_url = os.environ.get(
            "TAKO_VM_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/tako_vm_test"
        )
        schema = f"test_{uuid.uuid4().hex}"

        raw_parts = urlsplit(raw_db_url)
        raw_query = dict(parse_qsl(raw_parts.query, keep_blank_values=True))
        raw_query.pop("options", None)
        base_db_url = urlunsplit(
            (
                raw_parts.scheme,
                raw_parts.netloc,
                raw_parts.path,
                urlencode(raw_query),
                raw_parts.fragment,
            )
        )

        def with_schema(url: str, schema_name: str) -> str:
            parts = urlsplit(url)
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            query["options"] = f"-csearch_path={schema_name}"
            return urlunsplit(
                (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
            )

        try:
            with psycopg.connect(base_db_url, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema))
                    )
                    schema_created = True
        except psycopg.Error as exc:
            pytest.skip(f"PostgreSQL test database unavailable: {exc}")

        test_db_url = with_schema(base_db_url, schema)

        os.environ["TAKO_VM_DATA_DIR"] = tmpdir
        os.environ["TAKO_VM_DATABASE_URL"] = test_db_url

        reset_config()

        yield tmpdir

        reset_config()
        if original_data_dir:
            os.environ["TAKO_VM_DATA_DIR"] = original_data_dir
        else:
            os.environ.pop("TAKO_VM_DATA_DIR", None)
        if original_db_url:
            os.environ["TAKO_VM_DATABASE_URL"] = original_db_url
        else:
            os.environ.pop("TAKO_VM_DATABASE_URL", None)

        if schema_created:
            with psycopg.connect(base_db_url, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                    )


@pytest.fixture
def test_storage(temp_data_dir):
    """
    Create an ExecutionStorage instance for testing.

    Uses the temp_data_dir fixture for isolation.
    """
    import asyncio

    from tako_vm.storage import ExecutionStorage

    storage = ExecutionStorage(os.environ["TAKO_VM_DATABASE_URL"])
    loop = asyncio.new_event_loop()
    loop.run_until_complete(storage.init())

    class SyncStorageAdapter:
        def __init__(self, inner: ExecutionStorage):
            self._inner = inner
            self._loop = loop

        def _run(self, coro):
            return self._loop.run_until_complete(coro)

        def __getattr__(self, name):
            attr = getattr(self._inner, name)
            if not callable(attr):
                return attr

            def wrapper(*args, **kwargs):
                result = attr(*args, **kwargs)
                if inspect.isawaitable(result):
                    return self._run(result)
                return result

            return wrapper

    yield SyncStorageAdapter(storage)
    loop.run_until_complete(storage.close())
    loop.close()
