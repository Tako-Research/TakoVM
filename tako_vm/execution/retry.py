"""
Retry logic with exponential backoff for transient failures.

Provides utilities for retrying operations that may fail temporarily.
"""

import logging
import random
import time
from dataclasses import dataclass
from typing import Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class RetryConfig:
    """Configuration for retry behavior."""

    max_attempts: int = 3
    """Maximum number of attempts (including initial attempt)."""

    base_delay: float = 1.0
    """Base delay in seconds between retries."""

    max_delay: float = 30.0
    """Maximum delay in seconds between retries."""

    exponential_base: float = 2.0
    """Base for exponential backoff calculation."""

    jitter: bool = True
    """Add random jitter to prevent thundering herd."""

    jitter_factor: float = 0.1
    """Jitter factor (0.1 = +/- 10% of delay)."""


# Default set of transient errors that should trigger retry
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
    """
    Check if an error is likely transient and worth retrying.

    Args:
        error: The exception to check

    Returns:
        True if error appears to be transient
    """
    error_msg = str(error).lower()

    for pattern in TRANSIENT_ERROR_MESSAGES:
        if pattern in error_msg:
            return True

    # Check for specific exception types
    error_type = type(error).__name__.lower()
    transient_types = {"timeouterror", "connectionerror", "oserror"}
    if any(t in error_type for t in transient_types):
        return True

    return False


def calculate_delay(attempt: int, config: RetryConfig) -> float:
    """
    Calculate delay before next retry attempt.

    Uses exponential backoff with optional jitter.

    Args:
        attempt: Current attempt number (0-indexed)
        config: Retry configuration

    Returns:
        Delay in seconds
    """
    # Exponential backoff
    delay = config.base_delay * (config.exponential_base**attempt)

    # Cap at max delay
    delay = min(delay, config.max_delay)

    # Add jitter if enabled
    if config.jitter:
        jitter_range = delay * config.jitter_factor
        delay = delay + random.uniform(-jitter_range, jitter_range)

    return max(0, delay)


class RetryContext:
    """
    Context manager for retry logic.

    Useful when you need more control than the decorator provides.

    IMPORTANT: This class uses blocking time.sleep() for delays.
    It should only be used in synchronous code or within a ThreadPoolExecutor.
    Do NOT use this directly in async coroutines - the sleep will block
    the entire event loop.

    Example:
        # In synchronous code or thread pool:
        retry_ctx = RetryContext(RetryConfig(max_attempts=3))
        while retry_ctx.should_retry():
            try:
                result = do_something()
                break
            except Exception as e:
                retry_ctx.record_failure(e)
                if not retry_ctx.should_retry():
                    raise
    """

    def __init__(self, config: Optional[RetryConfig] = None):
        self.config = config or RetryConfig()
        self.attempt = 0
        self.last_error: Optional[Exception] = None

    def should_retry(self) -> bool:
        """Check if more retries are available."""
        return self.attempt < self.config.max_attempts

    def record_failure(self, error: Exception) -> None:
        """
        Record a failure and wait before next attempt.

        Args:
            error: The exception that occurred
        """
        self.last_error = error
        self.attempt += 1

        # Side effect: when another (transient) attempt remains, this blocks via
        # time.sleep for the backoff delay before returning. Callers run this in
        # synchronous code or a thread pool (see the class docstring), never on
        # an async event loop.
        if self.should_retry() and is_transient_error(error):
            delay = calculate_delay(self.attempt - 1, self.config)
            logger.info(
                f"Retry {self.attempt}/{self.config.max_attempts} after {delay:.2f}s: {error}"
            )
            time.sleep(delay)
