"""
Tako VM Command Line Interface.

Usage:
    tako-vm setup           Pull executor image and verify Docker is ready
    tako-vm doctor          Diagnose the local environment for readiness
    tako-vm server          Start the Tako VM server
    tako-vm dev up          Start local development services
    tako-vm dev status      Show local development services status
    tako-vm dev down        Stop local development services
    tako-vm status          Check server health
    tako-vm validate        Validate configuration file
    tako-vm config          Show current configuration
    tako-vm version         Show version
"""

# Suppress LibreSSL warnings on macOS before any other imports
import warnings

try:
    from urllib3.exceptions import NotOpenSSLWarning

    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)
except ImportError:
    pass

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/tako_vm"
MANAGED_POSTGRES_URL = "postgresql://postgres:postgres@localhost:55432/tako_vm"
MANAGED_POSTGRES_CONTAINER = "tako-vm-postgres"
MANAGED_POSTGRES_VOLUME = "tako-vm-postgres-data"


def _mask_database_url(url: str) -> str:
    """Mask the password in a database URL for display, keeping the username."""
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    creds, host = parts.netloc.rsplit("@", 1)
    username = creds.split(":", 1)[0] if creds else ""
    masked_creds = f"{username}:***" if username else "***"
    return urlunsplit(
        (parts.scheme, f"{masked_creds}@{host}", parts.path, parts.query, parts.fragment)
    )


def _postgres_container_running() -> bool:
    """Probe whether the managed PostgreSQL container is in the running state.

    Assumes the container exists (caller has already inspected it). Returns
    True only when `docker inspect` reports State.Running == true.
    """
    running_proc = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", MANAGED_POSTGRES_CONTAINER],
        check=True,
        capture_output=True,
        text=True,
    )
    return running_proc.stdout.strip().lower() == "true"


def main():
    parser = argparse.ArgumentParser(
        prog="tako-vm",
        description="Tako VM - Secure Python code execution",
    )

    # Global options
    parser.add_argument(
        "--config",
        "-c",
        type=Path,
        help="Path to configuration file (overrides default search paths)",
        metavar="FILE",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Server command
    server_parser = subparsers.add_parser("server", help="Start the Tako VM server")
    server_parser.add_argument(
        "--host", default=None, help="Host to bind to (default: from config, 0.0.0.0)"
    )
    server_parser.add_argument(
        "--port", type=int, default=None, help="Port to bind to (default: from config, 8000)"
    )
    server_parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload (cannot be combined with --workers > 1)",
    )
    server_parser.add_argument(
        "--workers",
        type=int,
        help=(
            "Number of worker processes (default: 1; cannot be combined with --reload). "
            "WARNING: each process runs its own in-memory worker pool, so async job "
            "polling relies on database fallbacks and wait=true result streaming only "
            "works on the submitting worker; prefer a single worker behind a load "
            "balancer."
        ),
    )
    server_parser.set_defaults(auto_start_postgres=True)
    server_parser.add_argument(
        "--no-auto-start-postgres",
        action="store_false",
        dest="auto_start_postgres",
        help="Disable auto-starting local PostgreSQL when using defaults",
    )

    # Dev command
    dev_parser = subparsers.add_parser("dev", help="Development helpers")
    dev_subparsers = dev_parser.add_subparsers(dest="dev_command", help="Dev commands")
    dev_up_parser = dev_subparsers.add_parser("up", help="Start local PostgreSQL for development")
    dev_up_parser.add_argument(
        "--with-server",
        action="store_true",
        help="Start API server after PostgreSQL is ready",
    )
    dev_up_parser.add_argument(
        "--host", default=None, help="Host to bind to (default: from config, 0.0.0.0)"
    )
    dev_up_parser.add_argument(
        "--port", type=int, default=None, help="Port to bind to (default: from config, 8000)"
    )
    dev_up_parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    dev_up_parser.set_defaults(auto_start_postgres=True)
    dev_subparsers.add_parser("status", help="Show local PostgreSQL status")
    dev_subparsers.add_parser("down", help="Stop local PostgreSQL container")

    # Status command
    status_parser = subparsers.add_parser("status", help="Check server health")
    status_parser.add_argument("--url", default="http://localhost:8000", help="Server URL")

    # Validate command
    validate_parser = subparsers.add_parser("validate", help="Validate configuration file")
    validate_parser.add_argument(
        "config_file",
        type=Path,
        nargs="?",
        help="Configuration file to validate (uses --config or default search if not specified)",
    )

    # Config command
    config_parser = subparsers.add_parser("config", help="Show current configuration")
    config_parser.add_argument("--json", action="store_true", help="Output as JSON")
    config_parser.add_argument(
        "--show-defaults", action="store_true", help="Show all values including defaults"
    )

    # Setup command
    subparsers.add_parser("setup", help="Pull executor image and verify Docker is ready")

    # Doctor command
    subparsers.add_parser(
        "doctor", help="Diagnose the local environment and report readiness to run jobs"
    )

    # Version command
    subparsers.add_parser("version", help="Show version")

    args = parser.parse_args()

    # Set global config path if provided
    if args.config:
        from tako_vm.config import set_config_path

        if not args.config.exists():
            print(f"Error: Config file not found: {args.config}", file=sys.stderr)
            sys.exit(1)
        set_config_path(args.config)

    if args.command == "server":
        run_server(args)
    elif args.command == "dev":
        if args.dev_command == "up":
            dev_up(args)
        elif args.dev_command == "down":
            dev_down(args)
        elif args.dev_command == "status":
            dev_status(args)
        else:
            dev_parser.print_help()
            sys.exit(1)
    elif args.command == "status":
        check_status(args)
    elif args.command == "validate":
        validate_config(args)
    elif args.command == "config":
        show_config(args)
    elif args.command == "setup":
        run_setup(args)
    elif args.command == "doctor":
        run_doctor(args)
    elif args.command == "version":
        from tako_vm import __version__

        print(f"tako-vm {__version__}")
    else:
        parser.print_help()
        sys.exit(1)


def run_setup(args):
    """Pull executor image and verify Docker is ready."""
    del args

    from tako_vm import __version__
    from tako_vm.constants import DEFAULT_IMAGE

    ghcr_image = f"ghcr.io/tako-research/takovm/executor:{__version__}"

    # Check Docker is available
    print("Checking Docker...")
    try:
        subprocess.run(
            ["docker", "info"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        print("Error: Docker is not installed.", file=sys.stderr)
        print("Install Docker: https://docs.docker.com/get-docker/", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError:
        print("Error: Docker is not running.", file=sys.stderr)
        sys.exit(1)
    print("  Docker is available")

    # Check if image already exists
    result = subprocess.run(
        ["docker", "image", "inspect", DEFAULT_IMAGE],
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        print(f"  Image '{DEFAULT_IMAGE}' already exists")
        print("\nSetup complete! Ready to run jobs.")
        return

    # Pull from GHCR
    print(f"Pulling {ghcr_image}...")
    result = subprocess.run(
        ["docker", "pull", ghcr_image],
        check=False,
    )
    if result.returncode != 0:
        # Fall back to latest tag
        ghcr_latest = "ghcr.io/tako-research/takovm/executor:latest"
        print(f"Version {__version__} not found, trying latest...")
        result = subprocess.run(
            ["docker", "pull", ghcr_latest],
            check=False,
        )
        if result.returncode != 0:
            print("Error: Failed to pull executor image.", file=sys.stderr)
            print(
                "Build manually: git clone https://github.com/Tako-Research/TakoVM.git && "
                "cd tako-vm && docker build -t code-executor:latest -f docker/Dockerfile.executor .",
                file=sys.stderr,
            )
            sys.exit(1)
        ghcr_image = ghcr_latest

    # Tag as the default image name
    subprocess.run(
        ["docker", "tag", ghcr_image, DEFAULT_IMAGE],
        check=True,
        capture_output=True,
    )
    print(f"  Tagged as '{DEFAULT_IMAGE}'")

    # Verify with a quick test
    print("Verifying...")
    try:
        result = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "python", DEFAULT_IMAGE, "-c", "print('ok')"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # A hung verification means the image is unusable; exit non-zero so
        # `tako-vm setup && ...` doesn't proceed on a broken image and report
        # success.
        print("Error: Image pulled but verification failed.", file=sys.stderr)
        print("  Verification timed out after 30s.", file=sys.stderr)
        sys.exit(1)
    if result.returncode == 0 and "ok" in result.stdout:
        print("  Executor image works")
    else:
        # The image is unusable, so exit non-zero so `tako-vm setup && ...`
        # doesn't proceed on a broken image and report success.
        print("Error: Image pulled but verification failed.", file=sys.stderr)
        if result.stderr:
            print(f"  {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    print("\nSetup complete! Ready to run jobs.")


def run_doctor(args):
    """Diagnose the local environment and report readiness to run jobs.

    Runs a set of independent checks (Docker, executor image, gVisor, database,
    workspace, config) and prints a single pass/warn/fail checklist so problems
    surface up front instead of as cryptic runtime errors mid-job. Exits non-zero
    if any blocking ([FAIL]) check fails, so it composes in scripts and CI.
    """
    del args

    from tako_vm.constants import DEFAULT_IMAGE, get_workspace_dir

    # (level, message, hint) — level in {"ok", "warn", "fail"}.
    results: list[tuple[str, str, str | None]] = []

    # Load config once up front; reused by the gVisor and database checks. The
    # result line is appended last so the checklist reads top-down by dependency.
    config = None
    config_result: tuple[str, str, str | None]
    try:
        from tako_vm.config import get_config

        config = get_config()
        config_result = ("ok", "Configuration valid", None)
    except Exception as e:
        config_result = ("fail", "Configuration invalid", str(e))

    # Are the server extras (psycopg, etc.) importable? Drives the DB check.
    try:
        import psycopg  # noqa: F401

        has_server_deps = True
    except ImportError:
        has_server_deps = False

    # --- Docker daemon ---
    docker_ok = False
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, text=True)
        docker_ok = True
        results.append(("ok", "Docker daemon running", None))
    except FileNotFoundError:
        results.append(
            ("fail", "Docker not installed", "Install Docker: https://docs.docker.com/get-docker/")
        )
    except subprocess.CalledProcessError:
        results.append(
            ("fail", "Docker installed but not running", "Start Docker Desktop or the daemon")
        )

    # --- Executor image ---
    if docker_ok:
        img = subprocess.run(
            ["docker", "image", "inspect", DEFAULT_IMAGE], capture_output=True, check=False
        )
        if img.returncode == 0:
            results.append(("ok", f"Executor image '{DEFAULT_IMAGE}' present", None))
        else:
            results.append(
                (
                    "fail",
                    f"Executor image '{DEFAULT_IMAGE}' missing",
                    "Run `tako-vm setup` to pull it (jobs fail without it)",
                )
            )
    else:
        results.append(("fail", "Executor image: skipped (Docker unavailable)", None))

    # --- gVisor runtime ---
    if docker_ok:
        try:
            from tako_vm.execution.worker import check_gvisor_available, reset_gvisor_check

            reset_gvisor_check()
            gvisor = check_gvisor_available()
        except Exception:
            gvisor = False
        strict = config is not None and config.security_mode == "strict"
        if gvisor:
            results.append(("ok", "gVisor (runsc) runtime available", None))
        elif strict:
            results.append(
                (
                    "fail",
                    "gVisor required by security_mode=strict but not available",
                    "Install gVisor, or set security_mode: permissive for local dev",
                )
            )
        else:
            results.append(
                (
                    "warn",
                    "gVisor (runsc) not available — permissive mode falls back to runc",
                    "Fine for local dev; for untrusted production set security_mode: strict",
                )
            )

    # --- Database ---
    db_url = config.database_url if config is not None else DEFAULT_DATABASE_URL
    if not has_server_deps:
        results.append(
            (
                "warn",
                "PostgreSQL client (psycopg) not installed",
                "Install server extras: pip install 'tako-vm[server]'",
            )
        )
    elif _can_connect_database(db_url):
        results.append(("ok", f"Database reachable at {_mask_database_url(db_url)}", None))
    elif _can_connect_database(MANAGED_POSTGRES_URL):
        results.append(
            (
                "ok",
                f"Managed dev PostgreSQL reachable at {_mask_database_url(MANAGED_POSTGRES_URL)}",
                None,
            )
        )
    else:
        results.append(
            (
                "warn",
                "No PostgreSQL reachable",
                "`tako-vm server` auto-starts one in dev mode, or run `tako-vm dev up`",
            )
        )

    # --- Workspace ---
    workspace = Path(get_workspace_dir())
    workspace_explicit = "TAKO_VM_WORKSPACE" in os.environ
    if not workspace.exists():
        results.append(
            (
                "fail",
                f"Workspace {workspace} does not exist",
                "Create it or point TAKO_VM_WORKSPACE at a writable directory",
            )
        )
    elif not os.access(workspace, os.W_OK):
        results.append(
            (
                "fail",
                f"Workspace {workspace} is not writable",
                "Point TAKO_VM_WORKSPACE at a writable directory",
            )
        )
    elif sys.platform == "darwin" and not workspace_explicit:
        # The default macOS temp dir (/var/folders/...) can't be bind-mounted into
        # the colima/Lima Docker VM, so job mounts fail at runtime with a cryptic
        # "bind source path does not exist". Warn before that happens.
        results.append(
            (
                "warn",
                f"Workspace defaults to the macOS system temp dir ({workspace})",
                "Set TAKO_VM_WORKSPACE to a dir under $HOME — the default can't be "
                "bind-mounted into the Docker VM",
            )
        )
    else:
        results.append(("ok", f"Workspace {workspace} is writable", None))

    # Config result reported last.
    results.append(config_result)

    # --- Render ---
    symbols = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}
    print("Tako VM environment check")
    print("=" * 40)
    for level, message, hint in results:
        print(f"  {symbols[level]}  {message}")
        if hint and level != "ok":
            print(f"          -> {hint}")

    fails = sum(1 for level, _, _ in results if level == "fail")
    warns = sum(1 for level, _, _ in results if level == "warn")
    print()
    if fails:
        print(
            f"{fails} blocking issue(s). Fix the [FAIL] items above, then re-run `tako-vm doctor`."
        )
        sys.exit(1)
    if warns:
        print(f"Ready to run, with {warns} warning(s). Start the server with `tako-vm server`.")
    else:
        print("All checks passed. Start the server with `tako-vm server`.")


def run_server(args):
    """Start the Tako VM server."""
    try:
        import uvicorn

        from tako_vm.config import ConfigurationError, get_config
        from tako_vm.server.app import app
    except ImportError:
        print("Error: Server dependencies not installed.")
        print("Install with: pip install tako-vm[server]")
        sys.exit(1)

    # Validate config before starting
    try:
        config = get_config()
        auto_start_postgres = bool(vars(args).get("auto_start_postgres", False))
        if auto_start_postgres:
            _auto_start_local_postgres_if_needed(config)
        print("Configuration loaded successfully")
        if config.production_mode:
            print("Running in PRODUCTION mode")
        else:
            print("Running in DEVELOPMENT mode")
    except ConfigurationError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    # Use CLI args if provided, otherwise fall back to config values
    host = args.host if args.host is not None else config.server_host
    port = args.port if args.port is not None else config.server_port

    workers = getattr(args, "workers", None)
    if workers is None:
        workers = 1
    if workers < 1:
        print("Error: --workers must be >= 1", file=sys.stderr)
        sys.exit(1)
    if args.reload and workers > 1:
        print(
            "Error: --reload cannot be combined with --workers > 1 "
            "(uvicorn's reloader manages a single worker process)",
            file=sys.stderr,
        )
        sys.exit(1)
    if workers > 1:
        print(
            f"WARNING: --workers {workers} runs {workers} independent worker pools; "
            "async job polling relies on database fallbacks and wait=true result "
            "streaming only works on the submitting worker. Prefer a single worker "
            "behind a load balancer unless you know you need this.",
            file=sys.stderr,
        )

    # uvicorn requires the app as an import string for --reload or multiple
    # workers (it silently disables them when given an app object). Those modes
    # spawn subprocesses that re-import the app, so carry an explicit --config
    # through the environment for get_config() in the child processes.
    use_import_string = args.reload or workers > 1
    if use_import_string:
        explicit_config = getattr(args, "config", None)
        if explicit_config:
            os.environ["TAKO_VM_CONFIG"] = str(explicit_config)

    uvicorn.run(
        "tako_vm.server.app:app" if use_import_string else app,
        host=host,
        port=port,
        reload=args.reload,
        workers=workers,
    )


def _can_connect_database(database_url: str, timeout: int = 2) -> bool:
    try:
        import psycopg

        with psycopg.connect(database_url, connect_timeout=timeout):
            return True
    except Exception as e:
        # Log the real reason so a bad URL / missing driver / auth failure
        # isn't indistinguishable from "database is down".
        logger.debug("Database connectivity check failed: %s", e)
        return False


def _ensure_managed_postgres() -> None:
    subprocess.run(["docker", "info"], check=True, capture_output=True, text=True)

    inspect_proc = subprocess.run(
        ["docker", "container", "inspect", MANAGED_POSTGRES_CONTAINER],
        check=False,
        capture_output=True,
        text=True,
    )

    if inspect_proc.returncode == 0:
        if not _postgres_container_running():
            subprocess.run(
                ["docker", "start", MANAGED_POSTGRES_CONTAINER],
                check=True,
                capture_output=True,
                text=True,
            )
    else:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                MANAGED_POSTGRES_CONTAINER,
                "-e",
                "POSTGRES_USER=postgres",
                "-e",
                "POSTGRES_PASSWORD=postgres",
                "-e",
                "POSTGRES_DB=tako_vm",
                "-p",
                "127.0.0.1:55432:5432",
                "-v",
                f"{MANAGED_POSTGRES_VOLUME}:/var/lib/postgresql/data",
                "postgres:16-alpine",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    deadline = time.time() + 60
    while time.time() < deadline:
        if _can_connect_database(MANAGED_POSTGRES_URL, timeout=2):
            return
        time.sleep(1)

    raise RuntimeError("Timed out waiting for local PostgreSQL to become ready")


def _managed_postgres_state() -> str:
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "docker_unavailable"

    inspect_proc = subprocess.run(
        ["docker", "container", "inspect", MANAGED_POSTGRES_CONTAINER],
        check=False,
        capture_output=True,
        text=True,
    )

    if inspect_proc.returncode != 0:
        return "missing"

    return "running" if _postgres_container_running() else "stopped"


def _auto_start_local_postgres_if_needed(config) -> None:
    disabled = os.environ.get("TAKO_VM_AUTO_START_LOCAL_POSTGRES", "1").lower() in {
        "0",
        "false",
        "no",
    }
    if disabled:
        return
    if config.production_mode:
        return
    if config.database_url != DEFAULT_DATABASE_URL:
        return
    if _can_connect_database(config.database_url):
        return

    try:
        print("Database unavailable; starting local PostgreSQL for development...")
        _ensure_managed_postgres()
    except Exception as e:
        print(
            f"Warning: failed to auto-start local PostgreSQL ({e}). "
            "Start a database manually or run `tako-vm dev up`.",
            file=sys.stderr,
        )
        return

    os.environ["TAKO_VM_DATABASE_URL"] = MANAGED_POSTGRES_URL
    config.database_url = MANAGED_POSTGRES_URL
    print(f"Using local PostgreSQL at {MANAGED_POSTGRES_URL}")


def dev_up(args):
    """Start local development services."""
    try:
        _ensure_managed_postgres()
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.strip() if e.stderr else str(e)
        print(f"Error: failed to start local PostgreSQL: {stderr}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: failed to start local PostgreSQL: {e}", file=sys.stderr)
        sys.exit(1)

    os.environ["TAKO_VM_DATABASE_URL"] = MANAGED_POSTGRES_URL
    print("Local PostgreSQL is ready")
    print(f"Database URL: {MANAGED_POSTGRES_URL}")

    if args.with_server:
        run_server(args)


def dev_down(args):
    """Stop local development services."""
    del args

    state = _managed_postgres_state()
    if state == "docker_unavailable":
        print("Error: Docker is not available", file=sys.stderr)
        sys.exit(1)
    if state == "missing":
        print("Local PostgreSQL container is not created")
        return
    if state == "stopped":
        print("Local PostgreSQL is already stopped")
        return

    subprocess.run(
        ["docker", "stop", MANAGED_POSTGRES_CONTAINER],
        check=True,
        capture_output=True,
        text=True,
    )
    print("Local PostgreSQL stopped")


def dev_status(args):
    """Show local development service status."""
    del args

    state = _managed_postgres_state()
    print("Development Services")
    print("=" * 20)
    print(f"Container: {MANAGED_POSTGRES_CONTAINER}")
    print(f"Database URL: {MANAGED_POSTGRES_URL}")

    if state == "docker_unavailable":
        print("Status: docker unavailable")
        sys.exit(1)
    if state == "missing":
        print("Status: not created")
        return
    if state == "stopped":
        print("Status: stopped")
        return

    reachable = _can_connect_database(MANAGED_POSTGRES_URL)
    print(f"Status: running ({'reachable' if reachable else 'not reachable'})")


def check_status(args):
    """Check server health status."""
    import requests

    try:
        response = requests.get(f"{args.url}/health", timeout=5)
        # A non-2xx /health means the server is up but unhealthy: surface the
        # status code and body instead of blindly parsing JSON (which could
        # raise, or worse, print a healthy-looking "unknown").
        if response.status_code >= 400:
            body = (response.text or "").strip()
            print(f"Error: server returned HTTP {response.status_code}", file=sys.stderr)
            if body:
                print(f"  {body[:500]}", file=sys.stderr)
            sys.exit(1)
        data = response.json()
        print(f"Status: {data.get('status', 'unknown')}")
        print(f"Docker: {'available' if data.get('docker_available') else 'unavailable'}")
        print(f"Version: {data.get('version', 'unknown')}")
    except requests.exceptions.ConnectionError:
        print(f"Error: Cannot connect to {args.url}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        # Reachable but the body wasn't valid JSON. Show what we got.
        print(f"Error: invalid JSON from {args.url}/health: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def validate_config(args):
    """Validate a configuration file."""
    from tako_vm.config import find_config_file, validate_config_file

    # Determine which file to validate
    config_file = args.config_file
    if config_file is None:
        # Use --config if provided, otherwise search
        if hasattr(args, "config") and args.config:
            config_file = args.config
        else:
            config_file = find_config_file()

    if config_file is None:
        print("No configuration file found.")
        print("Create tako_vm.yaml or specify a file with --config or as argument.")
        sys.exit(1)

    print(f"Validating: {config_file}")

    errors = validate_config_file(config_file)

    if errors:
        print("\nValidation FAILED:")
        for error in errors:
            print(f"  - {error}")
        sys.exit(1)
    else:
        print("Configuration is valid!")

        # Show summary
        try:
            from tako_vm.config import load_config

            config = load_config(config_file)
            print("\nSummary:")
            print(f"  Mode: {'production' if config.production_mode else 'development'}")
            print(f"  Workers: {config.max_workers}")
            print(f"  Max timeout: {config.max_timeout}s")
            print(f"  Job types defined: {len(config.job_types)}")
            if config.job_types:
                for jt in config.job_types:
                    print(f"    - {jt.name}")
        except (ImportError, ValueError, AttributeError) as e:
            # validate_config_file passed but the full loader (which also
            # applies env overrides + resolve_paths) failed; that divergence
            # matters, so don't hide it behind "Configuration is valid!".
            print(f"(could not render config summary: {e})", file=sys.stderr)


def show_config(args):
    """Show current configuration."""
    from tako_vm.config import ConfigurationError, get_config, get_config_path

    try:
        config = get_config()
    except ConfigurationError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    config_file = get_config_path()

    if args.json:
        import json

        # Export as JSON
        data = config.model_dump(
            exclude={
                "_resolved_data_dir",
                "_resolved_seccomp_profile_path",
            }
        )
        data["database_url"] = _mask_database_url(config.database_url)
        print(json.dumps(data, indent=2, default=str))
    else:
        print("Tako VM Configuration")
        print("=" * 50)
        if config_file:
            print(f"Config file: {config_file}")
        else:
            print("Config file: (using defaults)")
        print()

        print("[Mode]")
        print(f"  production_mode: {config.production_mode}")
        print()

        print("[Paths]")
        print(f"  data_dir: {config.data_dir}")
        print(f"  database_url: {_mask_database_url(config.database_url)}")
        print()

        print("[Queue & Workers]")
        print(f"  max_workers: {config.max_workers}")
        print(f"  max_queue_size: {config.max_queue_size}")
        print()

        print("[Limits]")
        print(f"  default_timeout: {config.default_timeout}s")
        print(f"  max_timeout: {config.max_timeout}s")
        print(f"  max_stdout_bytes: {config.max_stdout_bytes}")
        print(f"  max_code_bytes: {config.max_code_bytes}")
        print()

        print("[Container Limits]")
        limits = config.container_limits
        print(f"  nofile: {limits.nofile_soft}:{limits.nofile_hard}")
        print(f"  nproc: {limits.nproc_soft}:{limits.nproc_hard}")
        print(f"  fsize: {limits.fsize}")
        print(f"  tmpfs_size: {limits.tmpfs_size}")
        print(f"  pids_limit: {limits.pids_limit}")
        print()

        print("[Docker]")
        print(f"  docker_image: {config.docker_image}")
        print(f"  enable_seccomp: {config.enable_seccomp}")
        print(f"  enable_userns: {config.enable_userns}")
        print()

        if config.job_types:
            print("[Job Types]")
            for jt in config.job_types:
                print(f"  - {jt.name}:")
                print(
                    f"      memory: {jt.memory_limit}, cpu: {jt.cpu_limit}, timeout: {jt.timeout}s"
                )
                if jt.requirements:
                    print(f"      requirements: {', '.join(jt.requirements)}")


if __name__ == "__main__":
    main()
