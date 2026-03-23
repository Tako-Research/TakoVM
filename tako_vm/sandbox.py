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

from tako_vm.constants import DEFAULT_IMAGE, UV_CACHE_VOLUME, WORKSPACE_DIR
from tako_vm.execution.docker import generate_container_name, kill_container

logger = logging.getLogger(__name__)


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


@dataclass
class SandboxConfig:
    """Configuration for the sandbox."""

    image: str = DEFAULT_IMAGE
    """Docker image to use."""

    timeout: int = 30
    """Default timeout in seconds."""

    memory_limit: str = "512m"
    """Memory limit for containers."""

    cpu_limit: float = 1.0
    """CPU limit for containers."""

    network_enabled: bool = False
    """Whether to allow network access."""

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
        with Sandbox() as sb:
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
        package_dirs: Optional[List[str]] = None,
        auto_build: bool = True,
    ):
        """
        Initialize the sandbox.

        Args:
            image: Docker image to use (default: code-executor:latest)
            timeout: Default timeout in seconds
            memory_limit: Memory limit (e.g., "512m", "1g")
            cpu_limit: CPU limit (e.g., 1.0 = one CPU)
            network_enabled: Whether to allow network access
            package_dirs: Local directories to mount as Python packages
            auto_build: Whether to auto-build image if missing
        """
        self.config = SandboxConfig(
            image=image,
            timeout=timeout,
            memory_limit=memory_limit,
            cpu_limit=cpu_limit,
            network_enabled=network_enabled,
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
                f"  docker pull ghcr.io/las7/takovm/executor:latest && "
                f"docker tag ghcr.io/las7/takovm/executor:latest {self.config.image}\n"
                f"Or clone the repo and build it:\n"
                f"  git clone https://github.com/las7/tako-vm.git && "
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
                f"  docker pull ghcr.io/las7/takovm/executor:latest && "
                f"docker tag ghcr.io/las7/takovm/executor:latest {self.config.image}\n"
                f"Or clone the repo and build it:\n"
                f"  git clone https://github.com/las7/tako-vm.git && "
                f"cd tako-vm && docker build -t {self.config.image} -f docker/Dockerfile.executor ."
            )

        dockerfile = package_dir / "docker" / "Dockerfile.executor"
        if not dockerfile.exists():
            raise RuntimeError(
                f"Cannot auto-build image: Dockerfile not found at {dockerfile}. "
                f"Either pull the pre-built image:\n"
                f"  docker pull ghcr.io/las7/takovm/executor:latest && "
                f"docker tag ghcr.io/las7/takovm/executor:latest {self.config.image}\n"
                f"Or clone the repo and build it:\n"
                f"  git clone https://github.com/las7/tako-vm.git && "
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
            cmd, container_name = self._build_docker_command(
                code_dir=code_dir,
                input_dir=input_dir,
                output_dir=output_dir,
                timeout=timeout,
                requirements=requirements,
            )

            # Execute
            start_time = time.time()
            try:
                proc = subprocess.run(
                    cmd,
                    timeout=timeout + 5,  # Grace period for container overhead
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
                    except json.JSONDecodeError:
                        pass

                return SandboxResult(
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                    exit_code=proc.returncode,
                    success=proc.returncode == 0,
                    output=output_data,
                    duration_ms=duration_ms,
                )

            except subprocess.TimeoutExpired:
                # Kill the orphaned container (subprocess died but container keeps running)
                kill_container(container_name)
                duration_ms = int((time.time() - start_time) * 1000)
                return SandboxResult(
                    stdout="",
                    stderr="",
                    exit_code=-1,
                    success=False,
                    error=f"Execution timed out after {timeout}s",
                    duration_ms=duration_ms,
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
        # Generate container name for tracking (allows cleanup on timeout)
        container_name = generate_container_name("tako-sandbox")

        cmd = [
            "docker",
            "run",
            "--rm",
            f"--name={container_name}",
            "--init",  # Faster signal handling with tini
            "--read-only",
        ]

        # Capability restrictions (can be disabled in CI environments where Docker
        # can't modify capability bounding sets)
        if self.config.enable_cap_restrictions:
            cmd.extend(
                [
                    "--cap-drop=ALL",
                    "--cap-add=SETUID",  # Required for gosu to switch user
                    "--cap-add=SETGID",  # Required for gosu to switch user
                ]
            )
            # Security note: We don't use --security-opt=no-new-privileges because gosu requires
            # setuid to drop from root to sandbox user (uid 1000). This is a one-way privilege drop:
            # after gosu exec's the user code, the process runs as unprivileged sandbox user with
            # no capability to regain root. The container also has all other caps dropped.

        # Network isolation
        has_requirements = bool(requirements)
        if self.config.network_enabled or has_requirements:
            cmd.append("--network=bridge")
        else:
            cmd.append("--network=none")

        # Mount uv cache for faster installs
        if has_requirements:
            cmd.append(f"--mount=type=volume,source={UV_CACHE_VOLUME},target=/root/.cache/uv")

        # Resource limits
        cmd.extend(
            [
                f"--memory={self.config.memory_limit}",
                f"--memory-swap={self.config.memory_limit}",
                f"--cpus={self.config.cpu_limit}",
                "--pids-limit=100",
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
                logger.warning("Package directory does not exist: %s", pkg_dir)
                continue
            mount_target = f"/packages/pkg{i}"
            cmd.append(f"--mount=type=bind,source={pkg_path},target={mount_target},readonly")
            pythonpath_parts.append(mount_target)

        # Set PYTHONPATH if we have package directories
        if pythonpath_parts:
            pythonpath = ":".join(pythonpath_parts)
            cmd.append(f"--env=PYTHONPATH={pythonpath}")

        # Pass requirements
        if requirements:
            reqs_str = ",".join(requirements)
            cmd.append(f"--env=TAKO_REQUIREMENTS={reqs_str}")

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
