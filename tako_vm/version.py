"""
Job type versioning for Tako VM.

Provides deterministic versioning based on content hashes and optional
semantic version tags.
"""

import hashlib
import json
import logging
import subprocess
from datetime import datetime, timezone
from typing import Optional, Tuple

from .job_types import JobType
from .models import JobVersion
from .storage import ExecutionStorage

logger = logging.getLogger(__name__)


class VersionManager:
    """
    Manages job type versions and digests.

    Provides content-based hashing for reproducibility and optional
    semantic version aliases.
    """

    def __init__(self, storage: ExecutionStorage):
        """
        Initialize version manager.

        Args:
            storage: ExecutionStorage instance for persistence
        """
        self.storage = storage

    def compute_digest(self, job_type: JobType) -> str:
        """
        Compute content hash for job type configuration.

        The digest is based on all configuration that affects the
        container image, ensuring reproducibility.

        Args:
            job_type: JobType to compute digest for

        Returns:
            SHA256 hex digest
        """
        content = json.dumps(
            {
                "name": job_type.name,
                "requirements": sorted(job_type.requirements),
                "python_version": job_type.python_version,
                "base_image": job_type.base_image,
                "environment": dict(sorted(job_type.environment.items())),
                "shared_code": sorted(job_type.shared_code),
                "session_enabled": job_type.session_enabled,
                "gpu_enabled": job_type.gpu_enabled,
                "gpu_vendor": job_type.gpu_vendor,
                "gpu_count": job_type.gpu_count,
                "gpu_device_ids": sorted(job_type.gpu_device_ids),
            },
            sort_keys=True,
        )

        return hashlib.sha256(content.encode()).hexdigest()

    def compute_requirements_hash(self, job_type: JobType) -> str:
        """Compute hash of requirements list."""
        content = "\n".join(sorted(job_type.requirements))
        return hashlib.sha256(content.encode()).hexdigest()

    async def register_version(
        self,
        job_type: JobType,
        image_ref: str,
        version_tag: Optional[str] = None,
        built_by: Optional[str] = None,
        dockerfile_content: Optional[str] = None,
    ) -> JobVersion:
        """
        Register a built version.

        Args:
            job_type: JobType that was built
            image_ref: Docker image reference
            version_tag: Optional semantic version tag
            built_by: Who/what triggered the build
            dockerfile_content: Generated Dockerfile content

        Returns:
            Registered JobVersion
        """
        digest = self.compute_digest(job_type)

        dockerfile_hash = ""
        if dockerfile_content:
            dockerfile_hash = hashlib.sha256(dockerfile_content.encode()).hexdigest()

        version = JobVersion(
            job_type_name=job_type.name,
            version_tag=version_tag,
            digest=digest,
            built_at=datetime.now(timezone.utc),
            built_by=built_by,
            dockerfile_hash=dockerfile_hash,
            requirements_hash=self.compute_requirements_hash(job_type),
            image_ref=image_ref,
        )

        await self.storage.save_version(version)
        logger.info(f"Registered version {version.full_ref} (tag: {version_tag})")

        return version

    async def resolve(self, job_type_ref: str) -> Tuple[str, Optional[JobVersion]]:
        """
        Resolve job type reference to name and version.

        Supports formats:
        - "data-processing" -> (name, latest version)
        - "data-processing@v1.0.0" -> (name, tagged version)
        - "data-processing@sha256:abc123" -> (name, digest version)

        Args:
            job_type_ref: Job type reference string

        Returns:
            Tuple of (job_type_name, JobVersion or None)
        """
        if "@" not in job_type_ref:
            # No version specified - get latest
            version = await self.storage.get_latest_version(job_type_ref)
            return job_type_ref, version

        name, version_spec = job_type_ref.split("@", 1)

        if version_spec.startswith("sha256:"):
            # Digest reference
            digest = version_spec[7:]  # Remove 'sha256:' prefix
            version = await self.storage.get_version_by_digest(name, digest)
        else:
            # Tag reference (e.g., 'v1.0.0')
            version = await self.storage.get_version_by_tag(name, version_spec)

        return name, version

    def get_image_ref_for_version(self, version: JobVersion) -> str:
        """
        Get Docker image reference for version.

        Args:
            version: JobVersion to get image for

        Returns:
            Docker image reference
        """
        return version.image_ref

    def get_image_digest(self, image_name: str) -> Optional[str]:
        """
        Get Docker image digest.

        Args:
            image_name: Docker image name

        Returns:
            Image digest (sha256:...) or None if not found
        """
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", image_name, "--format", "{{index .RepoDigests 0}}"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            if result.returncode == 0 and result.stdout.strip():
                # Format: repo@sha256:digest
                ref = result.stdout.strip()
                if "@sha256:" in ref:
                    return ref.split("@")[1]  # Return sha256:...

            # Try getting ID as fallback
            result = subprocess.run(
                ["docker", "image", "inspect", image_name, "--format", "{{.Id}}"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            if result.returncode == 0:
                return result.stdout.strip()

        except subprocess.TimeoutExpired:
            logger.warning(f"Timeout getting digest for {image_name}")
        except Exception as e:
            logger.warning(f"Error getting digest for {image_name}: {e}")

        return None

    async def list_versions(self, job_type_name: str) -> list[JobVersion]:
        """
        List all versions for a job type.

        Args:
            job_type_name: Job type name

        Returns:
            List of JobVersions, newest first
        """
        return await self.storage.list_versions(job_type_name)

    async def version_exists(self, job_type_name: str, version_spec: str) -> bool:
        """
        Check if a version exists.

        Args:
            job_type_name: Job type name
            version_spec: Version tag or digest

        Returns:
            True if version exists
        """
        if version_spec.startswith("sha256:"):
            digest = version_spec[7:]
            return await self.storage.get_version_by_digest(job_type_name, digest) is not None
        else:
            return await self.storage.get_version_by_tag(job_type_name, version_spec) is not None


def parse_job_type_ref(job_type_ref: str) -> Tuple[str, Optional[str]]:
    """
    Parse job type reference into name and version.

    Args:
        job_type_ref: Reference like "data-processing" or "data-processing@v1.0.0"

    Returns:
        Tuple of (name, version_spec or None)
    """
    if "@" not in job_type_ref:
        return job_type_ref, None

    parts = job_type_ref.split("@", 1)
    return parts[0], parts[1]
