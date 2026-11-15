import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from tako_vm.execution.docker import CONTAINER_LABEL as EXECUTOR_CONTAINER_LABEL

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5

    recovery_timeout: float = 30.0

    success_threshold: int = 2


class DockerCircuitBreaker:
    def __init__(self, config: Optional[CircuitBreakerConfig] = None):
        self.config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: Optional[float] = None
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._state

    @property
    def is_available(self) -> bool:
        with self._lock:
            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.OPEN:
                if self._last_failure_time is not None:
                    elapsed = time.time() - self._last_failure_time
                    if elapsed >= self.config.recovery_timeout:
                        self._state = CircuitState.HALF_OPEN
                        self._success_count = 0
                        logger.info("Circuit breaker entering half-open state")
                        return True
                return False

            return True

    def record_success(self) -> None:
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.config.success_threshold:
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    logger.info("Circuit breaker closed (Docker recovered)")
            elif self._state == CircuitState.CLOSED:
                self._failure_count = 0

    def record_failure(self, error: Optional[str] = None) -> None:
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()

            if error:
                logger.warning(f"Docker operation failed: {error}")

            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                logger.warning("Circuit breaker re-opened (Docker still failing)")
            elif self._state == CircuitState.CLOSED:
                if self._failure_count >= self.config.failure_threshold:
                    self._state = CircuitState.OPEN
                    logger.error(f"Circuit breaker opened after {self._failure_count} failures")

    def check_docker_health(self) -> bool:
        try:
            result = subprocess.run(
                ["docker", "info"], capture_output=True, timeout=5.0, check=False
            )
            healthy = result.returncode == 0

            if healthy:
                self.record_success()
            else:
                self.record_failure("docker info returned non-zero")

            return healthy

        except subprocess.TimeoutExpired:
            self.record_failure("docker info timed out")
            return False
        except FileNotFoundError:
            self.record_failure("docker command not found")
            return False
        except Exception as e:
            self.record_failure(str(e))
            return False

    def get_status(self) -> dict:
        with self._lock:
            return {
                "state": self._state.value,
                "failure_count": self._failure_count,
                "success_count": self._success_count,
                "last_failure": self._last_failure_time,
            }


class DockerCleanup:
    CONTAINER_LABEL = EXECUTOR_CONTAINER_LABEL

    @staticmethod
    def _parse_created_at(created_at: str) -> Optional[float]:
        try:
            fields = created_at.strip().split(" ")
            dt = datetime.strptime(" ".join(fields[:3]), "%Y-%m-%d %H:%M:%S %z")
            return dt.timestamp()
        except (ValueError, IndexError):
            return None

    @classmethod
    def cleanup_orphaned_containers(cls, max_age_seconds: int = 3600) -> int:
        removed = 0

        try:
            result = subprocess.run(
                [
                    "docker",
                    "ps",
                    "-a",
                    "--filter",
                    f"label={cls.CONTAINER_LABEL}",
                    "--format",
                    "{{.ID}}\t{{.CreatedAt}}\t{{.State}}",
                ],
                capture_output=True,
                text=True,
                timeout=30.0,
                check=False,
            )

            if result.returncode != 0:
                logger.warning(f"Failed to list containers: {result.stderr}")
                return 0

            now = time.time()
            containers_to_remove = []

            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split("\t")
                if len(parts) < 3:
                    continue
                container_id, created_at, state = parts[0], parts[1], parts[2]

                if state in ("running", "restarting", "paused"):
                    created_ts = cls._parse_created_at(created_at)
                    if created_ts is None:
                        logger.warning(
                            f"Skipping running container {container_id}: "
                            f"unparseable CreatedAt {created_at!r}"
                        )
                        continue
                    age = now - created_ts
                    if age < max_age_seconds:
                        logger.debug(
                            f"Skipping running container {container_id} "
                            f"(age {age:.0f}s < {max_age_seconds}s)"
                        )
                        continue

                containers_to_remove.append(container_id)

            for container_id in containers_to_remove:
                try:
                    rm_result = subprocess.run(
                        ["docker", "rm", "-f", container_id],
                        capture_output=True,
                        timeout=10.0,
                        check=False,
                    )
                    if rm_result.returncode == 0:
                        removed += 1
                        logger.info(f"Removed orphaned container: {container_id}")
                except Exception as e:
                    logger.warning(f"Failed to remove container {container_id}: {e}")

            if removed > 0:
                logger.info(f"Cleaned up {removed} orphaned containers")

            return removed

        except subprocess.TimeoutExpired:
            logger.warning("Container cleanup timed out")
            return 0
        except Exception as e:
            logger.error(f"Container cleanup failed: {e}")
            return 0

    @classmethod
    def cleanup_dangling_images(cls) -> bool:
        try:
            result = subprocess.run(
                ["docker", "image", "prune", "-f"],
                capture_output=True,
                text=True,
                timeout=60.0,
                check=False,
            )

            if result.returncode == 0:
                output = result.stdout
                if "Total reclaimed space" in output:
                    logger.info(f"Image cleanup: {output.strip()}")
                return True

            logger.warning(f"Image cleanup returned non-zero: {result.stderr}")
            return False

        except Exception as e:
            logger.warning(f"Image cleanup failed: {e}")
            return False


_circuit_breaker: Optional[DockerCircuitBreaker] = None


def get_circuit_breaker() -> DockerCircuitBreaker:
    global _circuit_breaker
    if _circuit_breaker is None:
        _circuit_breaker = DockerCircuitBreaker()
    return _circuit_breaker


def startup_cleanup() -> dict:
    results = {
        "docker_healthy": False,
        "containers_removed": 0,
    }

    breaker = get_circuit_breaker()
    results["docker_healthy"] = breaker.check_docker_health()

    if results["docker_healthy"]:
        results["containers_removed"] = DockerCleanup.cleanup_orphaned_containers()

    return results
