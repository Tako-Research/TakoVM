"""
Container Builder - Build Docker images for job types.

This module handles building pre-configured Docker containers
for each job type with their required dependencies.

Example:
    from container_builder import ContainerBuilder
    from job_types import JobType

    builder = ContainerBuilder()
    builder.build(JobType(
        name="data-processing",
        requirements=["pandas", "numpy"],
    ))
"""

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from tako_vm.execution.docker import (
    DEFAULT_EXECUTOR_IMAGE,
    EXECUTOR_ENTRYPOINT,
    image_has_executor_entrypoint,
    reset_image_caches,
)
from tako_vm.execution.docker import image_exists as docker_image_exists
from tako_vm.job_types import JobType, JobTypeRegistry
from tako_vm.security import (
    validate_docker_image,
    validate_env_key,
    validate_env_value,
    validate_pip_requirement,
    validate_python_version,
)

logger = logging.getLogger(__name__)


class BuildError(Exception):
    """Raised when container build fails."""


class ContainerBuilder:
    """
    Builds Docker images for job types.
    """

    def __init__(self, custom_libs_path: Optional[Path] = None):
        """
        Initialize the builder.

        Args:
            custom_libs_path: Path to custom libraries directory
        """
        self.custom_libs_path = custom_libs_path or Path("custom_libs")

    def generate_dockerfile(self, job_type: JobType) -> str:
        """
        Generate Dockerfile content for a job type.

        The generated image derives from the executor base image (or an
        executor-derived ``base_image``) so it keeps the executor entrypoint
        contract (``ENTRYPOINT ["/entrypoint.sh"]``): the entrypoint enforces
        the in-container startup/execution timeouts, installs any per-job
        extra requirements, writes the phase/timing file, and runs
        ``/code/main.py`` itself as the sandbox user via gosu. The worker
        (``CodeExecutor._resolve_image``) refuses to run images that lack
        this contract, because docker would otherwise execute the image's
        default CMD instead of the user's code.

        Note: ``python_version`` no longer selects the default base image.
        The executor base image pins the Python version. To use a different
        Python, point ``base_image`` at a custom executor-derived image.

        Args:
            job_type: Job type configuration

        Returns:
            Dockerfile content as string

        Raises:
            BuildError: If job type has invalid configuration
        """
        # Validate python_version before using in base image
        if not validate_python_version(job_type.python_version):
            raise BuildError(
                f"Invalid python_version '{job_type.python_version}' for job type {job_type.name}"
            )

        # Determine and validate base image. The default is the executor base
        # image so the built image inherits /entrypoint.sh; a custom
        # base_image must itself be executor-derived or the worker will refuse
        # the built image at run time.
        if job_type.base_image:
            if not validate_docker_image(job_type.base_image):
                raise BuildError(
                    f"Invalid base_image '{job_type.base_image}' for job type {job_type.name}"
                )
            base_image = job_type.base_image
        else:
            base_image = DEFAULT_EXECUTOR_IMAGE

        # Build requirements install command with validation
        requirements_cmd = ""
        if job_type.requirements:
            validated_reqs = []
            for req in job_type.requirements:
                if not validate_pip_requirement(req):
                    logger.warning(
                        "Skipping invalid pip requirement in Dockerfile for %s: %s",
                        job_type.name,
                        req,
                    )
                    continue
                validated_reqs.append(req)

            if validated_reqs:
                req_list = " ".join(f'"{r}"' for r in validated_reqs)
                requirements_cmd = f"RUN uv pip install --system --no-cache {req_list}"

        # Build environment variables (with validation and escaping)
        env_lines = ""
        if job_type.environment:
            for key, value in job_type.environment.items():
                # Validate key and value to prevent injection
                if not validate_env_key(key):
                    logger.warning("Skipping invalid env key in Dockerfile: %s", key)
                    continue
                if not validate_env_value(value):
                    logger.warning("Skipping env with unsafe value in Dockerfile: %s", key)
                    continue
                # Escape quotes and backslashes in value for Dockerfile
                escaped_value = value.replace("\\", "\\\\").replace('"', '\\"')
                env_lines += f'ENV {key}="{escaped_value}"\n'

        dockerfile = f"""# Auto-generated Dockerfile for job type: {job_type.name}
# Derives from the executor base image so the built image keeps the executor
# entrypoint contract (ENTRYPOINT /entrypoint.sh, inherited from the base).
FROM {base_image}

# Install uv for fast dependency installation (already present on the
# executor base; kept so custom executor-derived bases get it too).
# Digest-pinned to match docker/Dockerfile.executor; bump both together.
COPY --from=ghcr.io/astral-sh/uv:0.5.14@sha256:f0786ad49e2e684c18d38697facb229f538a6f5e374c56f54125aabe7d14b3f7 /uv /usr/local/bin/uv

# Install custom libraries if present
COPY ./custom_libs /tmp/custom_libs
RUN if [ -n "$(ls -A /tmp/custom_libs/*.whl 2>/dev/null)" ]; then \\
        uv pip install --system --no-cache /tmp/custom_libs/*.whl; \\
    fi && \\
    rm -rf /tmp/custom_libs

# Install job type requirements at BUILD time. The worker skips runtime
# installation of job_type.requirements when running this image.
{requirements_cmd}

# Copy shared code if present
COPY ./shared_code /app/shared_code
ENV PYTHONPATH="/app/shared_code:$PYTHONPATH"

# Environment variables
{env_lines}

# Ensure the sandbox user and the mount-point dirs exist (no-ops on the
# executor base, which already provides them)
RUN id -u sandbox >/dev/null 2>&1 || useradd -m -u 1000 sandbox
RUN mkdir -p /code /input /output /tmp && \\
    chown sandbox:sandbox /output /tmp && \\
    chmod 755 /code /input && \\
    chmod 777 /output /tmp

WORKDIR /app

# Deliberately NO USER/CMD/ENTRYPOINT overrides: the container must start as
# container root so the inherited /entrypoint.sh can install per-job extra
# requirements and write the phase file, then drop to the sandbox user (gosu)
# to run /code/main.py. A `USER sandbox` or CMD here would break or mask that
# contract (the worker would refuse the image, or the wrong process would run).
"""
        return dockerfile

    def build(self, job_type: JobType, no_cache: bool = False, quiet: bool = False) -> bool:
        """
        Build Docker image for a job type.

        Args:
            job_type: Job type configuration
            no_cache: If True, build without Docker cache
            quiet: If True, suppress build output

        Returns:
            True if build succeeded

        Raises:
            BuildError: If build fails
        """
        logger.info(f"Building container for job type: {job_type.name}")

        # Create temporary build context
        with tempfile.TemporaryDirectory() as build_dir:
            build_path = Path(build_dir)

            # Write Dockerfile
            dockerfile_content = self.generate_dockerfile(job_type)
            dockerfile_path = build_path / "Dockerfile"
            dockerfile_path.write_text(dockerfile_content, encoding="utf-8")

            # Prepare the build context (copy custom_libs / shared_code). Wrap
            # the filesystem work so an OSError here surfaces as a BuildError,
            # letting build_all record a single failure for this job type
            # instead of aborting the whole batch.
            try:
                # Copy custom_libs
                custom_libs_dest = build_path / "custom_libs"
                custom_libs_dest.mkdir()
                if self.custom_libs_path.exists():
                    for item in self.custom_libs_path.iterdir():
                        if item.is_file():
                            shutil.copy2(item, custom_libs_dest)

                # Copy shared code with path validation
                shared_code_dest = build_path / "shared_code"
                shared_code_dest.mkdir()
                # Get current working directory as the allowed base for shared_code
                allowed_base = Path.cwd().resolve()
                for code_path in job_type.shared_code:
                    src = Path(code_path).resolve()
                    # Security: Ensure shared_code paths don't escape the allowed directory
                    try:
                        src.relative_to(allowed_base)
                    except ValueError:
                        logger.warning(
                            "Skipping shared_code path that escapes allowed directory: %s",
                            code_path,
                        )
                        continue
                    if src.exists():
                        if src.is_file():
                            shutil.copy2(src, shared_code_dest)
                        else:
                            shutil.copytree(src, shared_code_dest / src.name)
            except OSError as e:
                logger.error("Failed to prepare build context for %s: %s", job_type.name, e)
                raise BuildError(f"Failed to prepare build context for {job_type.name}: {e}") from e

            # Build the image
            cmd = ["docker", "build", "-t", job_type.image_name]
            if no_cache:
                cmd.append("--no-cache")
            cmd.append(str(build_path))

            try:
                # Generous timeout: image builds can install many deps, but an
                # unbounded subprocess.run could hang the caller forever.
                subprocess.run(cmd, capture_output=not quiet, text=True, check=True, timeout=1800)
                logger.info("Successfully built image: %s", job_type.image_name)
                # The tag now points at new content: drop any cached inspect
                # results so the worker re-verifies the rebuilt image.
                reset_image_caches()
                # Verify the executor entrypoint contract survived the build.
                # This only fails for a custom non-executor base_image; warn
                # loudly because the worker will refuse to run such an image.
                if image_has_executor_entrypoint(job_type.image_name) is not True:
                    logger.warning(
                        "Built image %s does NOT carry the executor entrypoint contract "
                        "(ENTRYPOINT %s); the worker will refuse to run it. base_image "
                        "'%s' for job type '%s' must derive from the executor image "
                        "(docker/Dockerfile.executor).",
                        job_type.image_name,
                        EXECUTOR_ENTRYPOINT,
                        job_type.base_image,
                        job_type.name,
                    )
                return True
            except subprocess.TimeoutExpired as e:
                logger.error("Build timed out for %s after %ss", job_type.name, e.timeout)
                raise BuildError(
                    f"Failed to build {job_type.name}: docker build timed out after {e.timeout}s"
                ) from e
            except subprocess.CalledProcessError as e:
                error_msg = e.stderr if e.stderr else str(e)
                logger.error("Failed to build image: %s", error_msg)
                raise BuildError(f"Failed to build {job_type.name}: {error_msg}") from e

    def build_all(
        self,
        registry: JobTypeRegistry,
        no_cache: bool = False,
        quiet: bool = False,
        skip_existing: bool = False,
    ) -> dict[str, bool]:
        """
        Build images for all registered job types.

        Args:
            registry: Job type registry
            no_cache: If True, build without Docker cache
            quiet: If True, suppress build output
            skip_existing: If True, skip job types whose image already exists
                on the Docker daemon instead of rebuilding it. Useful for
                long-running callers that pre-build images at startup and only
                want to build what is missing.

        Returns:
            Dict mapping job type name to build success
        """
        results = {}
        for job_type in registry.list():
            if skip_existing and self.image_exists(job_type):
                logger.info(f"Image for {job_type.name} already exists; skipping build")
                results[job_type.name] = True
                continue
            try:
                self.build(job_type, no_cache=no_cache, quiet=quiet)
                results[job_type.name] = True
            except BuildError as e:
                logger.error(f"Failed to build {job_type.name}: {e}")
                results[job_type.name] = False
        return results

    def image_exists(self, job_type: JobType) -> bool:
        """
        Check if image for job type exists.

        Args:
            job_type: Job type configuration

        Returns:
            True if image exists
        """
        # Delegate to the shared, timeout-bounded, cached existence check so the
        # builder, worker, and sandbox all probe the daemon identically (the
        # bare subprocess version here had no timeout and could hang).
        return docker_image_exists(job_type.image_name)

    def remove_image(self, job_type: JobType) -> bool:
        """
        Remove image for job type.

        Args:
            job_type: Job type configuration

        Returns:
            True if removed successfully
        """
        try:
            subprocess.run(["docker", "rmi", job_type.image_name], capture_output=True, check=True)
            return True
        except subprocess.CalledProcessError as e:
            logger.warning("Failed to remove image %s: %s", job_type.image_name, e.stderr or e)
            return False


def build_job_type_cli():
    """Command-line interface for building job types."""
    import argparse

    parser = argparse.ArgumentParser(description="Build job type containers")
    parser.add_argument("name", nargs="?", help="Job type name to build (or 'all')")
    parser.add_argument("--list", action="store_true", help="List all job types")
    parser.add_argument("--no-cache", action="store_true", help="Build without cache")
    parser.add_argument("--init-defaults", action="store_true", help="Initialize default job types")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    from tako_vm.job_types import init_default_job_types

    registry = JobTypeRegistry()

    if args.init_defaults:
        init_default_job_types(registry)
        print("Initialized default job types")

    if args.list:
        print("\nRegistered job types:")
        for jt in registry.list():
            status = "✓" if ContainerBuilder().image_exists(jt) else "✗"
            print(f"  [{status}] {jt.name}: {jt.requirements}")
        return

    if args.name:
        builder = ContainerBuilder()

        if args.name == "all":
            results = builder.build_all(registry, no_cache=args.no_cache)
            print("\nBuild results:")
            for name, success in results.items():
                status = "✓" if success else "✗"
                print(f"  [{status}] {name}")
        else:
            job_type = registry.get(args.name)
            if not job_type:
                print(f"Job type '{args.name}' not found")
                return
            builder.build(job_type, no_cache=args.no_cache)


if __name__ == "__main__":
    build_job_type_cli()
