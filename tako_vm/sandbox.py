"""
Direct Docker sandbox for running code without a server.

This module provides a simple, library-like interface for running Python code
in isolated Docker containers. No server required.

Example:
    from tako_vm import Sandbox

    with Sandbox() as sb:
        result = sb.run("print(1 + 1)")
        print(result.stdout)  # "2"
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from tako_vm.config import get_config
from tako_vm.constants import (
    DEFAULT_IMAGE,
    MAX_REQUIREMENTS,
    UV_CACHE_TMP_DIR,
    UV_CACHE_VOLUME,
    UV_CACHE_VOLUME_DIR,
    WORKSPACE_DIR,
)
from tako_vm.execution import resolve_runtime
from tako_vm.execution.docker import (
    base_isolation_args,
    generate_container_name,
    inspect_oom_killed,
    remove_container,
    ulimit_args,
)
from tako_vm.security import validate_pip_requirement

logger = logging.getLogger(__name__)

_SAFE_PROXY_URL_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:/=,+-[]{}@%"
)

DEFAULT_STARTUP_TIMEOUT = 120
"""Default startup (dependency install) timeout in seconds, matching server defaults."""


def _decode_stream(value: Any) -> str:
    """Decode partial subprocess output that may be None, bytes, or str.

    subprocess.TimeoutExpired carries raw bytes even when subprocess.run
    was invoked with text=True.
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


@dataclass
class SandboxResult:
    """Result from a sandbox execution."""

    stdout: str = ""
    """Standard output from the execution."""

    stderr: str = ""
    """Standard error from the execution."""

    exit_code: int = 0
    """Exit code from the container (0 = success)."""

    success: bool = True
    """Whether the execution succeeded."""

    output: Optional[Dict[str, Any]] = None
    """Parsed JSON from /output/result.json if present."""

    error: Optional[str] = None
    """Error message if execution failed."""

    duration_ms: Optional[int] = None
    """Execution duration in milliseconds."""


def _default_enable_cap_restrictions() -> bool:
    """Get default value for enable_cap_restrictions from env var."""
    env_val = os.environ.get("TAKO_VM_ENABLE_CAP_RESTRICTIONS", "true").lower()
    return env_val in ("true", "1", "yes")


def _validate_dependency_proxy_url(value: Optional[str]) -> Optional[str]:
    """Validate optional dependency proxy URL for Docker env usage."""
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if any(char in normalized for char in ("\n", "\r", "\x00")):
        raise ValueError("dependency_proxy_url cannot contain control characters")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https", "socks5"}:
        raise ValueError("dependency_proxy_url must use http://, https://, or socks5://")
    if not parsed.hostname:
        raise ValueError("dependency_proxy_url must include a hostname")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("dependency_proxy_url cannot include a path, query, or fragment")
    if any(char not in _SAFE_PROXY_URL_CHARS for char in normalized):
        raise ValueError("dependency_proxy_url contains unsupported characters")
    return normalized


@dataclass
class SandboxConfig:
    """Configuration for the sandbox."""

    image: str = DEFAULT_IMAGE
    """Docker image to use."""

    timeout: int = 30
    """Default timeout in seconds."""

    startup_timeout: int = DEFAULT_STARTUP_TIMEOUT
    """Timeout in seconds for the startup phase (runtime dependency installation)."""

    memory_limit: str = "512m"
    """Memory limit for containers."""

    cpu_limit: float = 1.0
    """CPU limit for containers."""

    network_enabled: bool = False
    """Whether to allow network access."""

    allow_runtime_requirements: bool = False
    """Whether to allow installing requirements at runtime."""

    dependency_proxy_url: Optional[str] = None
    """Optional proxy URL used only during runtime dependency installs."""

    enable_runtime_dependency_cache: bool = False
    """Whether to mount a shared uv cache volume for runtime dependency installs."""

    package_dirs: List[str] = field(default_factory=list)
    """Local directories to mount as packages (added to PYTHONPATH)."""

    enable_cap_restrictions: bool = field(default_factory=_default_enable_cap_restrictions)
    """Enable capability restrictions (--cap-drop=ALL --cap-add=...)."""


class Sandbox:
    """
    Direct Docker sandbox for running code without a server.

    This provides a simple, library-like interface for running Python code
    in isolated Docker containers. The sandbox handles:

    - Docker image management (auto-builds if needed)
    - Container lifecycle (create, run, cleanup)
    - Security configuration (isolation, resource limits)
    - Package management (requirements, local packages)

    Example:
        # Basic usage
        with Sandbox() as sb:
            result = sb.run("print(1 + 1)")
            print(result.stdout)  # "2"

        # With dependencies
        with Sandbox(allow_runtime_requirements=True) as sb:
            result = sb.run(
                "import pandas; print(pandas.__version__)",
                requirements=["pandas"]
            )

        # With local packages
        sb = Sandbox(package_dirs=["./my_utils"])
        result = sb.run("from my_utils import helper; helper.do_thing()")
    """

    def __init__(
        self,
        image: str = DEFAULT_IMAGE,
        timeout: int = 30,
        memory_limit: str = "512m",
        cpu_limit: float = 1.0,
        network_enabled: bool = False,
        allow_runtime_requirements: bool = False,
        dependency_proxy_url: Optional[str] = None,
        enable_runtime_dependency_cache: bool = False,
        package_dirs: Optional[List[str]] = None,
        auto_build: bool = True,
        startup_timeout: int = DEFAULT_STARTUP_TIMEOUT,
    ):
        """
        Initialize the sandbox.

        Args:
            image: Docker image to use (default: code-executor:latest)
            timeout: Default timeout in seconds
            memory_limit: Memory limit (e.g., "512m", "1g")
            cpu_limit: CPU limit (e.g., 1.0 = one CPU)
            network_enabled: Whether to allow network access
            allow_runtime_requirements: Whether requirements may be installed at runtime
            dependency_proxy_url: Optional proxy URL for runtime dependency installs
            enable_runtime_dependency_cache: Whether to use a shared uv cache volume
            package_dirs: Local directories to mount as Python packages
            auto_build: Whether to auto-build image if missing
            startup_timeout: Timeout in seconds for runtime dependency installation
        """
        self.config = SandboxConfig(
            image=image,
            timeout=timeout,
            startup_timeout=startup_timeout,
            memory_limit=memory_limit,
            cpu_limit=cpu_limit,
            network_enabled=network_enabled,
            allow_runtime_requirements=allow_runtime_requirements,
            dependency_proxy_url=_validate_dependency_proxy_url(dependency_proxy_url),
            enable_runtime_dependency_cache=enable_runtime_dependency_cache,
            package_dirs=package_dirs or [],
        )
        self.auto_build = auto_build
        self._image_checked = False

    def __enter__(self) -> "Sandbox":
        """Context manager entry."""
        self._ensure_image()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        pass

    def _ensure_image(self) -> None:
        """Ensure the Docker image exists, building if necessary."""
        if self._image_checked:
            return

        # Check if image exists
        result = subprocess.run(
            ["docker", "image", "inspect", self.config.image],
            capture_output=True,
            check=False,
        )

        if result.returncode == 0:
            self._image_checked = True
            return

        if not self.auto_build:
            raise RuntimeError(
                f"Docker image '{self.config.image}' not found. "
                f"Either pull the pre-built image:\n"
                f"  docker pull ghcr.io/tako-research/takovm/executor:latest && "
                f"docker tag ghcr.io/tako-research/takovm/executor:latest {self.config.image}\n"
                f"Or clone the repo and build it:\n"
                f"  git clone https://github.com/Tako-Research/TakoVM.git && "
                f"cd tako-vm && docker build -t {self.config.image} -f docker/Dockerfile.executor ."
            )

        # Try to build the image
        logger.info("Docker image '%s' not found, building...", self.config.image)
        self._build_image()
        self._image_checked = True

    def _build_image(self) -> None:
        """Build the executor Docker image."""
        # Find the tako-vm package directory
        package_dir = self._find_package_dir()
        if not package_dir:
            raise RuntimeError(
                f"Cannot auto-build image: tako-vm source directory not found. "
                f"Either pull the pre-built image:\n"
                f"  docker pull ghcr.io/tako-research/takovm/executor:latest && "
                f"docker tag ghcr.io/tako-research/takovm/executor:latest {self.config.image}\n"
                f"Or clone the repo and build it:\n"
                f"  git clone https://github.com/Tako-Research/TakoVM.git && "
                f"cd tako-vm && docker build -t {self.config.image} -f docker/Dockerfile.executor ."
            )

        dockerfile = package_dir / "docker" / "Dockerfile.executor"
        if not dockerfile.exists():
            raise RuntimeError(
                f"Cannot auto-build image: Dockerfile not found at {dockerfile}. "
                f"Either pull the pre-built image:\n"
                f"  docker pull ghcr.io/tako-research/takovm/executor:latest && "
                f"docker tag ghcr.io/tako-research/takovm/executor:latest {self.config.image}\n"
                f"Or clone the repo and build it:\n"
                f"  git clone https://github.com/Tako-Research/TakoVM.git && "
                f"cd tako-vm && docker build -t {self.config.image} -f docker/Dockerfile.executor ."
            )

        print(f"Building executor image '{self.config.image}'... (one-time setup)")

        result = subprocess.run(
            [
                "docker",
                "build",
                "-t",
                self.config.image,
                "-f",
                str(dockerfile),
                str(package_dir),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            raise RuntimeError(f"Failed to build Docker image:\n{result.stderr}")

        print("Image built successfully.")

    def _find_package_dir(self) -> Optional[Path]:
        """Find the tako-vm package directory for building."""
        # Try to find relative to this file
        this_file = Path(__file__).resolve()
        package_dir = this_file.parent.parent  # tako_vm -> tako-vm

        # Check if docker/Dockerfile.executor exists
        if (package_dir / "docker" / "Dockerfile.executor").exists():
            return package_dir

        # Try current working directory
        cwd = Path.cwd()
        if (cwd / "docker" / "Dockerfile.executor").exists():
            return cwd

        return None

    def run(
        self,
        code: str,
        input_data: Optional[Dict[str, Any]] = None,
        timeout: Optional[int] = None,
        requirements: Optional[List[str]] = None,
    ) -> SandboxResult:
        """
        Run Python code in the sandbox.

        Args:
            code: Python code to execute
            input_data: Input data available as /input/data.json
            timeout: Timeout in seconds (overrides default)
            requirements: Python packages to install (e.g., ["pandas", "numpy>=1.20"])

        Returns:
            SandboxResult with stdout, stderr, exit_code, and output

        Example:
            result = sandbox.run('''
            import json
            with open('/input/data.json') as f:
                data = json.load(f)
            result = sum(data['numbers'])
            with open('/output/result.json', 'w') as f:
                json.dump({'sum': result}, f)
            print(f"Sum: {result}")
            ''', input_data={'numbers': [1, 2, 3, 4, 5]})

            print(result.stdout)  # "Sum: 15"
            print(result.output)  # {'sum': 15}
        """
        self._ensure_image()

        timeout = timeout or self.config.timeout
        input_data = input_data or {}

        # Create temporary workspace
        workspace = Path(tempfile.mkdtemp(prefix="sandbox-", dir=WORKSPACE_DIR))

        try:
            # Prepare directories
            code_dir = workspace / "code"
            input_dir = workspace / "input"
            output_dir = workspace / "output"

            code_dir.mkdir()
            input_dir.mkdir()
            output_dir.mkdir()
            output_dir.chmod(0o777)

            # Write code
            code_file = code_dir / "main.py"
            code_file.write_text(code)
            code_file.chmod(0o444)

            # Write input data
            input_file = input_dir / "data.json"
            input_file.write_text(json.dumps(input_data))
            input_file.chmod(0o444)

            # Build docker command
            try:
                cmd, container_name = self._build_docker_command(
                    code_dir=code_dir,
                    input_dir=input_dir,
                    output_dir=output_dir,
                    timeout=timeout,
                    requirements=requirements,
                )
            except ValueError as e:
                return SandboxResult(success=False, exit_code=-1, error=str(e))

            # Execute. The container enforces its own timeouts via
            # TAKO_STARTUP_TIMEOUT / TAKO_EXECUTION_TIMEOUT; this subprocess
            # timeout is a backstop with a grace period for container overhead.
            # Dependency installation happens before code runs, so budget the
            # startup phase separately when requirements are present.
            subprocess_timeout = timeout + 5
            if requirements:
                subprocess_timeout += self.config.startup_timeout

            start_time = time.time()
            # The container runs without --rm (see _build_docker_command), so
            # this try/finally — entered only once `docker run` is attempted —
            # owns removal on every exit path: success, failure, OOM, host
            # timeout, and unexpected exceptions. remove_container does
            # `docker rm -f`, which also kills a still-running container (the
            # TimeoutExpired case, where subprocess.run killed the docker CLI
            # but the container kept running in the daemon).
            try:
                try:
                    proc = subprocess.run(
                        cmd,
                        timeout=subprocess_timeout,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    duration_ms = int((time.time() - start_time) * 1000)

                    # Read output
                    output_data = None
                    output_file = output_dir / "result.json"
                    if output_file.exists():
                        try:
                            output_data = json.loads(output_file.read_text())
                        except (json.JSONDecodeError, OSError, ValueError) as e:
                            # Output existed but was unreadable/unparseable.
                            # Don't silently present None as "no output".
                            logger.warning(
                                "result.json for %s was not valid JSON, ignoring: %s",
                                container_name,
                                e,
                            )

                    # The in-container timeout (TAKO_EXECUTION_TIMEOUT, enforced
                    # by entrypoint.sh via timeout(1)) exits 124 when the code
                    # exceeds its budget, so most timeouts return here rather
                    # than through the TimeoutExpired backstop below. Surface
                    # them as timeouts.
                    error = None
                    if proc.returncode == 124:
                        error = f"Execution timed out after {timeout}s"
                    elif proc.returncode == 137:
                        # Exit 137 is SIGKILL — could be the OOM killer, but
                        # also `docker kill` or user code calling
                        # sys.exit(137). Only the exited container's
                        # State.OOMKilled distinguishes them; inspect before
                        # the finally block removes the container. In-container
                        # timeout kills are already remapped to 124 by the
                        # entrypoint, so a 137 seen here is a genuine SIGKILL.
                        # None (inspect failed) falls back to reporting OOM so
                        # a flaky inspect never loses a true OOM — same policy
                        # as the server-side CodeExecutor.
                        oom_killed = inspect_oom_killed(container_name)
                        if oom_killed is False:
                            error = "Process was killed (SIGKILL) but not by the memory limit"
                        else:
                            error = (
                                "Container killed: out of memory "
                                f"(memory_limit={self.config.memory_limit})"
                            )

                    return SandboxResult(
                        stdout=proc.stdout,
                        stderr=proc.stderr,
                        exit_code=proc.returncode,
                        success=proc.returncode == 0,
                        output=output_data,
                        duration_ms=duration_ms,
                        error=error,
                    )

                except subprocess.TimeoutExpired as exc:
                    duration_ms = int((time.time() - start_time) * 1000)
                    # Reaching the host backstop means the in-container timeout
                    # (TAKO_EXECUTION_TIMEOUT) did NOT fire — daemon hung, heavy
                    # container overhead, or a stuck startup phase. Report the
                    # real budget consumed (subprocess_timeout), not the inner
                    # code timeout, and say which limit tripped.
                    logger.warning(
                        "Sandbox %s hit the host timeout backstop after %ss "
                        "(in-container timeout did not fire)",
                        container_name,
                        subprocess_timeout,
                    )
                    return SandboxResult(
                        stdout=_decode_stream(exc.stdout),
                        stderr=_decode_stream(exc.stderr),
                        exit_code=-1,
                        success=False,
                        error=(
                            f"Execution killed by host backstop after "
                            f"{subprocess_timeout}s (in-container timeout did not fire)"
                        ),
                        duration_ms=duration_ms,
                    )
            finally:
                # Best-effort kill+remove (`docker rm -f`). A failure here leaks
                # a sandbox container; the labeled orphan cleanup at startup is
                # the backstop, but surface it so the leak isn't invisible.
                if not remove_container(container_name):
                    logger.warning(
                        "Failed to remove sandbox container %s; relying on startup orphan cleanup",
                        container_name,
                    )

        finally:
            # Cleanup
            try:
                shutil.rmtree(workspace)
            except Exception as e:
                logger.warning("Failed to cleanup workspace: %s", e)

    def _build_docker_command(
        self,
        code_dir: Path,
        input_dir: Path,
        output_dir: Path,
        timeout: int,
        requirements: Optional[List[str]] = None,
    ) -> Tuple[List[str], str]:
        """Build the docker run command with security flags.

        Returns:
            Tuple of (command, container_name) for cleanup on timeout.
        """
        validated_reqs = []
        if requirements:
            # Enforce the policy before validation so an all-invalid list
            # cannot bypass the allow_runtime_requirements check.
            if not self.config.allow_runtime_requirements:
                raise ValueError(
                    "Runtime dependency installation is disabled. "
                    "Use pre-built images or set allow_runtime_requirements=True."
                )
            if len(requirements) > MAX_REQUIREMENTS:
                raise ValueError(
                    f"Too many requirements ({len(requirements)} > {MAX_REQUIREMENTS})"
                )
            for req in requirements:
                if not validate_pip_requirement(req):
                    raise ValueError(f"Invalid pip requirement: {req!r}")
                validated_reqs.append(req)

        if validated_reqs:
            requirements_file = input_dir / "_requirements.txt"
            requirements_file.write_text("\n".join(validated_reqs) + "\n", encoding="utf-8")
            requirements_file.chmod(0o444)

        # Generate container name for tracking (allows cleanup on timeout)
        container_name = generate_container_name("tako-sandbox")

        # Shared isolation base: --init/--read-only, capability drops, the
        # tako-vm-executor label (so startup cleanup can find orphans), and the
        # gVisor --runtime flag, resolved via the shared resolver so the
        # library path enforces the same posture as CodeExecutor (and fails
        # closed in strict mode when gVisor is unavailable). Library-mode runs
        # have no ExecutionRecord, so the unique container name doubles as the
        # traceability ID.
        #
        # auto_remove=False: keep the exited container so a 137 exit can be
        # checked against `docker inspect .State.OOMKilled` (with --rm the
        # daemon removes the container before it can be inspected). The
        # try/finally in run() guarantees removal on every exit path.
        cmd = base_isolation_args(
            container_name,
            runtime=resolve_runtime(get_config()),
            enable_cap_restrictions=self.config.enable_cap_restrictions,
            execution_id=container_name,
            auto_remove=False,
        )

        # Network isolation
        has_requirements = bool(validated_reqs)
        if self.config.network_enabled or has_requirements:
            cmd.append("--network=bridge")
        else:
            cmd.append("--network=none")

        # Mount uv cache for faster installs
        if has_requirements:
            uv_cache_dir = UV_CACHE_TMP_DIR
            if self.config.enable_runtime_dependency_cache:
                uv_cache_dir = UV_CACHE_VOLUME_DIR
                cmd.append(f"--mount=type=volume,source={UV_CACHE_VOLUME},target={uv_cache_dir}")
            cmd.append(f"--env=UV_CACHE_DIR={uv_cache_dir}")

        # Resource limits. The pids-limit and the --ulimit set (nofile/nproc/
        # fsize) come from the shared container_limits config so the library
        # path enforces the same kernel rlimits the worker does. Without the
        # --ulimit=fsize cap, untrusted code could write an arbitrarily large
        # file to the writable /output bind-mount (host-disk-exhaustion DoS);
        # gVisor does not impose RLIMIT_FSIZE on its own (issue #97).
        limits = get_config().container_limits
        cmd.extend(
            [
                f"--memory={self.config.memory_limit}",
                f"--memory-swap={self.config.memory_limit}",
                f"--cpus={self.config.cpu_limit}",
                f"--pids-limit={limits.pids_limit}",
                *ulimit_args(limits),
            ]
        )

        # Mount directories
        # Use larger /tmp when requirements need to be installed (packages go to /tmp/site-packages)
        tmp_size = "300m" if has_requirements else "100m"
        cmd.extend(
            [
                f"--mount=type=bind,source={code_dir.absolute()},target=/code,readonly",
                f"--mount=type=bind,source={input_dir.absolute()},target=/input,readonly",
                f"--mount=type=bind,source={output_dir.absolute()},target=/output",
                f"--tmpfs=/tmp:rw,exec,nosuid,size={tmp_size}",
            ]
        )

        # Mount local package directories
        pythonpath_parts = []
        for i, pkg_dir in enumerate(self.config.package_dirs):
            pkg_path = Path(pkg_dir).absolute()
            if not pkg_path.exists():
                # The caller explicitly asked for this dependency; silently
                # dropping it would surface later as a confusing in-sandbox
                # ModuleNotFoundError instead of at the misconfiguration site.
                raise ValueError(f"Package directory does not exist: {pkg_dir}")
            mount_target = f"/packages/pkg{i}"
            cmd.append(f"--mount=type=bind,source={pkg_path},target={mount_target},readonly")
            pythonpath_parts.append(mount_target)

        # Set PYTHONPATH if we have package directories
        if pythonpath_parts:
            pythonpath = ":".join(pythonpath_parts)
            cmd.append(f"--env=PYTHONPATH={pythonpath}")

        if has_requirements and self.config.dependency_proxy_url:
            cmd.append(f"--env=TAKO_DEPENDENCY_PROXY_URL={self.config.dependency_proxy_url}")

        # In-container timeout enforcement (entrypoint.sh wraps each phase in
        # timeout(1)). Without these the container would run forever if the
        # parent process died before its subprocess timeout fired.
        cmd.append(f"--env=TAKO_STARTUP_TIMEOUT={self.config.startup_timeout}")
        cmd.append(f"--env=TAKO_EXECUTION_TIMEOUT={timeout}")

        # Image
        cmd.append(self.config.image)

        return cmd, container_name


# Convenience function for simple usage
def run(
    code: str,
    input_data: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
    requirements: Optional[List[str]] = None,
    **kwargs,
) -> SandboxResult:
    """
    Run Python code in an isolated sandbox.

    This is a convenience function that creates a temporary Sandbox,
    runs the code, and returns the result.

    Args:
        code: Python code to execute
        input_data: Input data available as /input/data.json
        timeout: Timeout in seconds
        requirements: Python packages to install
        **kwargs: Additional arguments passed to Sandbox()

    Returns:
        SandboxResult with stdout, stderr, exit_code, and output

    Example:
        from tako_vm.sandbox import run

        result = run("print(1 + 1)")
        print(result.stdout)  # "2"
    """
    with Sandbox(timeout=timeout, **kwargs) as sb:
        return sb.run(code, input_data=input_data, requirements=requirements)
