import logging
import random
import time
from dataclasses import dataclass
from typing import Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class RetryConfig:
    max_attempts: int = 3

    base_delay: float = 1.0

    max_delay: float = 30.0

    exponential_base: float = 2.0

    jitter: bool = True

    jitter_factor: float = 0.1


TRANSIENT_ERROR_MESSAGES: Set[str] = {
    "connection refused",
    "connection reset",
    "connection timed out",
    "temporary failure",
    "service unavailable",
    "resource temporarily unavailable",
    "too many requests",
    "rate limit",
    "docker daemon",
    "cannot connect to docker",
    "no space left on device",
}


def is_transient_error(error: Exception) -> bool:
    error_msg = str(error).lower()

    for pattern in TRANSIENT_ERROR_MESSAGES:
        if pattern in error_msg:
            return True

    error_type = type(error).__name__.lower()
    transient_types = {"timeouterror", "connectionerror", "oserror"}
    if any(t in error_type for t in transient_types):
        return True

    return False


def calculate_delay(attempt: int, config: RetryConfig) -> float:
    delay = config.base_delay * (config.exponential_base**attempt)

    delay = min(delay, config.max_delay)

    if config.jitter:
        jitter_range = delay * config.jitter_factor
        delay = delay + random.uniform(-jitter_range, jitter_range)

    return max(0, delay)


class RetryContext:
    def __init__(self, config: Optional[RetryConfig] = None):
        self.config = config or RetryConfig()
        self.attempt = 0
        self.last_error: Optional[Exception] = None

    def should_retry(self) -> bool:
        return self.attempt < self.config.max_attempts

    def record_failure(self, error: Exception) -> None:
        self.last_error = error
        self.attempt += 1

        if self.should_retry() and is_transient_error(error):
            delay = calculate_delay(self.attempt - 1, self.config)
            logger.info(
                f"Retry {self.attempt}/{self.config.max_attempts} after {delay:.2f}s: {error}"
            )
            time.sleep(delay)
