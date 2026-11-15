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
    UV_CACHE_VOLUME_DIR,
    get_workspace_dir,
    uv_cache_volume,
)
from tako_vm.execution import resolve_runtime
from tako_vm.execution.docker import (
    base_isolation_args,
    classify_docker_run_result,
    classify_sigkill,
    decode_subprocess_stream,
    generate_container_name,
    image_exists,
    is_native_linux,
    prepare_requirements_file,
    read_result_json,
    remove_container,
    ulimit_args,
)
from tako_vm.security import validate_docker_run_args

logger = logging.getLogger(__name__)

_SAFE_PROXY_URL_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:/=,+-[]{}@%"
)

DEFAULT_STARTUP_TIMEOUT = 120


@dataclass
class SandboxResult:
    stdout: str = ""

    stderr: str = ""

    exit_code: int = 0

    success: bool = True

    output: Optional[Dict[str, Any]] = None

    error: Optional[str] = None

    duration_ms: Optional[int] = None


def _default_enable_cap_restrictions() -> bool:
    env_val = os.environ.get("TAKO_VM_ENABLE_CAP_RESTRICTIONS", "true").lower()
    return env_val in ("true", "1", "yes")


def _validate_dependency_proxy_url(value: Optional[str]) -> Optional[str]:
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
    image: str = DEFAULT_IMAGE

    timeout: int = 30

    startup_timeout: int = DEFAULT_STARTUP_TIMEOUT

    memory_limit: str = "512m"

    cpu_limit: float = 1.0

    network_enabled: bool = False

    allow_runtime_requirements: bool = False

    dependency_proxy_url: Optional[str] = None

    enable_runtime_dependency_cache: bool = False

    package_dirs: List[str] = field(default_factory=list)

    enable_cap_restrictions: bool = field(default_factory=_default_enable_cap_restrictions)

    cache_scope: str = "default"


class Sandbox:
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
        self._ensure_image()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        pass

    def _ensure_image(self) -> None:
        if self._image_checked:
            return

        if image_exists(self.config.image):
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

        logger.info("Docker image '%s' not found, building...", self.config.image)
        self._build_image()
        self._image_checked = True

    def _build_image(self) -> None:
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
        this_file = Path(__file__).resolve()
        package_dir = this_file.parent.parent

        if (package_dir / "docker" / "Dockerfile.executor").exists():
            return package_dir

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
        self._ensure_image()

        timeout = timeout or self.config.timeout
        input_data = input_data or {}

        workspace = Path(tempfile.mkdtemp(prefix="sandbox-", dir=get_workspace_dir()))

        try:
            code_dir = workspace / "code"
            input_dir = workspace / "input"
            output_dir = workspace / "output"

            code_dir.mkdir()
            input_dir.mkdir()
            output_dir.mkdir()
            output_dir.chmod(0o777)

            code_file = code_dir / "main.py"
            code_file.write_text(code)
            code_file.chmod(0o444)

            input_file = input_dir / "data.json"
            input_file.write_text(json.dumps(input_data))
            input_file.chmod(0o444)

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

            subprocess_timeout = timeout + 5
            if requirements:
                subprocess_timeout += self.config.startup_timeout

            start_time = time.time()
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

                    is_infra_failure, detail = classify_docker_run_result(
                        proc.returncode, proc.stderr or ""
                    )
                    if is_infra_failure:
                        logger.warning(
                            "Sandbox %s hit a Docker infrastructure failure: %s",
                            container_name,
                            detail,
                        )
                        return SandboxResult(
                            stdout=proc.stdout,
                            stderr=proc.stderr,
                            exit_code=proc.returncode,
                            success=False,
                            duration_ms=duration_ms,
                            error=f"Docker infrastructure failure: {detail}",
                        )

                    output_data = read_result_json(output_dir, container_name)

                    error = None
                    if proc.returncode == 124:
                        error = f"Execution timed out after {timeout}s"
                    elif proc.returncode == 137:
                        oom_killed = classify_sigkill(container_name)
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
                    logger.warning(
                        "Sandbox %s hit the host timeout backstop after %ss "
                        "(in-container timeout did not fire)",
                        container_name,
                        subprocess_timeout,
                    )
                    return SandboxResult(
                        stdout=decode_subprocess_stream(exc.stdout),
                        stderr=decode_subprocess_stream(exc.stderr),
                        exit_code=-1,
                        success=False,
                        error=(
                            f"Execution killed by host backstop after "
                            f"{subprocess_timeout}s (in-container timeout did not fire)"
                        ),
                        duration_ms=duration_ms,
                    )
            finally:
                if not remove_container(container_name):
                    logger.warning(
                        "Failed to remove sandbox container %s; relying on startup orphan cleanup",
                        container_name,
                    )

        finally:
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
        validated_reqs = prepare_requirements_file(
            requirements,
            input_dir,
            allow_runtime_requirements=self.config.allow_runtime_requirements,
            max_requirements=MAX_REQUIREMENTS,
        )

        container_name = generate_container_name("tako-sandbox")

        cmd = base_isolation_args(
            container_name,
            runtime=resolve_runtime(get_config()),
            enable_cap_restrictions=self.config.enable_cap_restrictions,
            execution_id=container_name,
            auto_remove=False,
        )

        has_requirements = bool(validated_reqs)
        if self.config.network_enabled or has_requirements:
            cmd.append("--network=bridge")
        else:
            cmd.append("--network=none")

        if has_requirements:
            uv_cache_dir = UV_CACHE_TMP_DIR
            if self.config.enable_runtime_dependency_cache:
                uv_cache_dir = UV_CACHE_VOLUME_DIR
                cache_volume = uv_cache_volume(self.config.cache_scope)
                cmd.append(f"--mount=type=volume,source={cache_volume},target={uv_cache_dir}")
            cmd.append(f"--env=UV_CACHE_DIR={uv_cache_dir}")

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

        tmp_size = "300m" if has_requirements else str(limits.tmpfs_size)
        tmp_exec = "exec" if has_requirements else "noexec"
        cmd.extend(
            [
                f"--mount=type=bind,source={code_dir.absolute()},target=/code,readonly",
                f"--mount=type=bind,source={input_dir.absolute()},target=/input,readonly",
                f"--mount=type=bind,source={output_dir.absolute()},target=/output",
                f"--tmpfs=/tmp:rw,{tmp_exec},nosuid,size={tmp_size}",
            ]
        )

        cfg = get_config()
        if cfg.enable_seccomp and cfg.seccomp_profile_path:
            if is_native_linux() and cfg.seccomp_profile_path.exists():
                cmd.append(f"--security-opt=seccomp={cfg.seccomp_profile_path}")
            elif not is_native_linux():
                logger.debug("Skipping custom seccomp profile on Docker Desktop")

        pythonpath_parts = []
        for i, pkg_dir in enumerate(self.config.package_dirs):
            pkg_path = Path(pkg_dir).absolute()
            if not pkg_path.exists():
                raise ValueError(f"Package directory does not exist: {pkg_dir}")
            mount_target = f"/packages/pkg{i}"
            cmd.append(f"--mount=type=bind,source={pkg_path},target={mount_target},readonly")
            pythonpath_parts.append(mount_target)

        if pythonpath_parts:
            pythonpath = ":".join(pythonpath_parts)
            cmd.append(f"--env=PYTHONPATH={pythonpath}")

        if has_requirements and self.config.dependency_proxy_url:
            cmd.append(f"--env=TAKO_DEPENDENCY_PROXY_URL={self.config.dependency_proxy_url}")

        cmd.append(f"--env=TAKO_STARTUP_TIMEOUT={self.config.startup_timeout}")
        cmd.append(f"--env=TAKO_EXECUTION_TIMEOUT={timeout}")

        cmd.append(self.config.image)

        if not validate_docker_run_args(cmd):
            raise ValueError("Unsafe Docker command rejected: arguments failed safety validation")

        return cmd, container_name


def run(
    code: str,
    input_data: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
    requirements: Optional[List[str]] = None,
    **kwargs,
) -> SandboxResult:
    with Sandbox(timeout=timeout, **kwargs) as sb:
        return sb.run(code, input_data=input_data, requirements=requirements)
