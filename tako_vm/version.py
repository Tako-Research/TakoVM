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
    def __init__(self, storage: ExecutionStorage):
        self.storage = storage

    def compute_digest(self, job_type: JobType) -> str:
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
        if "@" not in job_type_ref:
            version = await self.storage.get_latest_version(job_type_ref)
            return job_type_ref, version

        name, version_spec = job_type_ref.split("@", 1)

        if version_spec.startswith("sha256:"):
            digest = version_spec[7:]
            version = await self.storage.get_version_by_digest(name, digest)
        else:
            version = await self.storage.get_version_by_tag(name, version_spec)

        return name, version

    def get_image_ref_for_version(self, version: JobVersion) -> str:
        return version.image_ref

    def get_image_digest(self, image_name: str) -> Optional[str]:
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", image_name, "--format", "{{index .RepoDigests 0}}"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            if result.returncode == 0 and result.stdout.strip():
                ref = result.stdout.strip()
                if "@sha256:" in ref:
                    return ref.split("@")[1]

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
        return await self.storage.list_versions(job_type_name)

    async def version_exists(self, job_type_name: str, version_spec: str) -> bool:
        if version_spec.startswith("sha256:"):
            digest = version_spec[7:]
            return await self.storage.get_version_by_digest(job_type_name, digest) is not None
        else:
            return await self.storage.get_version_by_tag(job_type_name, version_spec) is not None


def parse_job_type_ref(job_type_ref: str) -> Tuple[str, Optional[str]]:
    if "@" not in job_type_ref:
        return job_type_ref, None

    parts = job_type_ref.split("@", 1)
    return parts[0], parts[1]
