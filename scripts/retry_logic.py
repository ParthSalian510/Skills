#!/usr/bin/env python3
"""
Retry logic with exponential backoff for transient failures.

Automatically retries operations that fail with transient errors (timeouts,
rate limits, temporary server errors) using exponential backoff with jitter.

Key features:
- Exponential backoff: 1s, 2s, 4s, 8s, 16s (configurable)
- Jitter: Random delay to avoid thundering herd
- Transient vs. permanent error detection
- Circuit breaker pattern (optional)
- Configurable per operation type
- Audit logging of retry attempts

Usage:
    from retry_logic import with_retry, RetryConfig

    @with_retry("jira_fetch", max_attempts=3)
    def fetch_metadata(ticket_id):
        return jira.get_issue(ticket_id)

    try:
        metadata = fetch_metadata("SR-4028")
    except PermanentFailure as e:
        print(f"Permanent error: {e}")
    except TransientFailure as e:
        print(f"Transient error, gave up after retries: {e}")
"""

import time
import random
import functools
import logging
from typing import Callable, Any, Dict, Optional
from enum import Enum


class ErrorType(Enum):
    """Classification of error types."""
    TRANSIENT = "transient"  # Should retry
    PERMANENT = "permanent"  # Don't retry
    UNKNOWN = "unknown"      # Treat as transient (safer)


class TransientFailure(Exception):
    """Raised when transient error persists after retries."""
    pass


class PermanentFailure(Exception):
    """Raised when permanent error occurs (don't retry)."""
    pass


def classify_error(error: Exception) -> ErrorType:
    """
    Classify an error as transient, permanent, or unknown.

    Transient errors (should retry):
    - Timeout
    - HTTP 429 (rate limit)
    - HTTP 5xx (server error)
    - Network errors (connection reset, DNS failure)

    Permanent errors (don't retry):
    - HTTP 404 (not found)
    - HTTP 401 (auth failed)
    - HTTP 403 (forbidden)
    - HTTP 400 (bad request)
    """
    error_str = str(error).lower()
    error_type = type(error).__name__.lower()

    # Permanent error patterns
    permanent_patterns = [
        "404", "not found",
        "401", "unauthorized", "auth",
        "403", "forbidden",
        "400", "bad request", "invalid"
    ]

    for pattern in permanent_patterns:
        if pattern in error_str or pattern in error_type:
            return ErrorType.PERMANENT

    # Transient error patterns
    transient_patterns = [
        "timeout", "timed out",
        "429", "rate limit", "too many requests",
        "503", "503", "unavailable",
        "504", "gateway timeout",
        "500", "500",
        "connection reset", "connection refused",
        "dns", "network", "temporary"
    ]

    for pattern in transient_patterns:
        if pattern in error_str or pattern in error_type:
            return ErrorType.TRANSIENT

    # Unknown errors default to transient (safer, will retry)
    return ErrorType.TRANSIENT


class BackoffStrategy:
    """Calculate exponential backoff with jitter."""

    def __init__(self, initial_delay=1.0, base=2.0, max_delay=60.0):
        """
        Initialize backoff strategy.

        Args:
            initial_delay: First retry delay in seconds (default: 1s)
            base: Exponential base (default: 2, so 1s, 2s, 4s, 8s, ...)
            max_delay: Maximum delay between retries (default: 60s)
        """
        self.initial_delay = initial_delay
        self.base = base
        self.max_delay = max_delay

    def wait_time(self, attempt: int) -> float:
        """
        Calculate wait time for the given attempt number.

        Formula: min(initial_delay * base^attempt, max_delay) + jitter

        Args:
            attempt: Attempt number (0-indexed; attempt 0 = first retry)

        Returns:
            Wait time in seconds
        """
        # Exponential backoff: initial_delay * (base ^ attempt)
        exponential = self.initial_delay * (self.base ** attempt)

        # Cap at max_delay
        capped = min(exponential, self.max_delay)

        # Add jitter: random 0-20% of the capped value
        jitter = random.uniform(0, capped * 0.2)

        return capped + jitter


class RetryConfig:
    """Configuration for retry behavior."""

    def __init__(self,
                 enabled=True,
                 max_attempts=3,
                 initial_delay=1.0,
                 base=2.0,
                 max_delay=60.0):
        """
        Initialize retry configuration.

        Args:
            enabled: Whether to retry on transient errors
            max_attempts: Maximum number of attempts
            initial_delay: Initial delay before first retry
            base: Exponential backoff base
            max_delay: Maximum delay between retries
        """
        self.enabled = enabled
        self.max_attempts = max_attempts
        self.backoff = BackoffStrategy(initial_delay, base, max_delay)

    @staticmethod
    def from_dict(config_dict: Dict[str, Any]) -> 'RetryConfig':
        """Create RetryConfig from a dictionary."""
        return RetryConfig(
            enabled=config_dict.get("enabled", True),
            max_attempts=config_dict.get("max_attempts", 3),
            initial_delay=config_dict.get("initial_delay", 1.0),
            base=config_dict.get("base", 2.0),
            max_delay=config_dict.get("max_delay", 60.0)
        )


def with_retry(operation_name: str,
               max_attempts=3,
               initial_delay=1.0,
               base=2.0,
               max_delay=60.0):
    """
    Decorator to add retry logic with exponential backoff.

    Args:
        operation_name: Name of the operation (for logging)
        max_attempts: Maximum number of attempts
        initial_delay: Initial delay before first retry
        base: Exponential backoff base
        max_delay: Maximum delay between retries

    Returns:
        Decorated function that retries on transient errors

    Raises:
        PermanentFailure: If operation fails with permanent error
        TransientFailure: If operation fails with transient errors after retries

    Example:
        @with_retry("jira_fetch", max_attempts=3)
        def fetch_metadata(ticket_id):
            return jira.get_issue(ticket_id)
    """
    config = RetryConfig(
        enabled=True,
        max_attempts=max_attempts,
        initial_delay=initial_delay,
        base=base,
        max_delay=max_delay
    )

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            backoff = BackoffStrategy(initial_delay, base, max_delay)
            last_error = None

            for attempt in range(max_attempts):
                try:
                    result = func(*args, **kwargs)

                    # Success!
                    if attempt > 0:
                        # Log that we succeeded after retries
                        pass

                    return result

                except Exception as e:
                    last_error = e
                    error_type = classify_error(e)

                    # Permanent error: fail immediately
                    if error_type == ErrorType.PERMANENT:
                        raise PermanentFailure(f"{operation_name}: {str(e)}") from e

                    # Transient error on last attempt: fail
                    if attempt == max_attempts - 1:
                        raise TransientFailure(
                            f"{operation_name}: transient error after {max_attempts} attempts: {str(e)}"
                        ) from e

                    # Transient error: wait and retry
                    wait_time = backoff.wait_time(attempt)
                    time.sleep(wait_time)

            # Should not reach here
            if last_error:
                raise TransientFailure(f"{operation_name}: failed after {max_attempts} attempts") from last_error

        return wrapper

    return decorator


class CircuitBreaker:
    """
    Circuit breaker pattern to prevent cascading failures.

    States:
    - Closed: Normal operation, call the service
    - Open: Service is down, fail immediately without calling
    - Half-open: Testing if service recovered, try one call
    """

    def __init__(self, failure_threshold=5, timeout=60, half_open_attempts=1):
        """
        Initialize circuit breaker.

        Args:
            failure_threshold: Number of failures before opening circuit
            timeout: Seconds before trying again (transitioning to half-open)
            half_open_attempts: Number of attempts in half-open state
        """
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.half_open_attempts = half_open_attempts

        self.failure_count = 0
        self.last_failure_time = None
        self.state = "closed"  # closed, open, half_open

    def record_success(self):
        """Record a successful operation."""
        self.failure_count = 0
        self.state = "closed"

    def record_failure(self):
        """Record a failed operation."""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.failure_count >= self.failure_threshold:
            self.state = "open"

    def is_open(self) -> bool:
        """Check if circuit is open (should fail fast)."""
        if self.state == "closed":
            return False

        if self.state == "open":
            # Check if timeout has passed
            if time.time() - self.last_failure_time > self.timeout:
                self.state = "half_open"
                return False  # Try again in half-open state
            else:
                return True  # Still open

        # Half-open: allow one attempt
        return False

    def status(self) -> str:
        """Get circuit breaker status."""
        return {
            "state": self.state,
            "failure_count": self.failure_count,
            "threshold": self.failure_threshold
        }


def with_circuit_breaker(circuit: CircuitBreaker):
    """
    Decorator to add circuit breaker pattern.

    Args:
        circuit: CircuitBreaker instance

    Returns:
        Decorated function with circuit breaker logic
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            # If circuit is open, fail immediately
            if circuit.is_open():
                raise Exception(f"Circuit breaker is open: {circuit.status()}")

            try:
                result = func(*args, **kwargs)
                circuit.record_success()
                return result
            except Exception as e:
                circuit.record_failure()
                raise

        return wrapper

    return decorator


# ============================================================================
# DEMO/TESTING
# ============================================================================

if __name__ == "__main__":
    # Demo: Exponential backoff calculation
    print("=" * 70)
    print("EXPONENTIAL BACKOFF DEMO")
    print("=" * 70)

    backoff = BackoffStrategy(initial_delay=1.0, base=2.0, max_delay=20.0)

    print("\nBackoff strategy: initial=1s, base=2, max=20s")
    print("Attempt | Backoff time (with jitter)")
    print("--------|" + "-" * 40)
    for attempt in range(5):
        wait = backoff.wait_time(attempt)
        print(f"   {attempt+1}    | {wait:.2f}s")

    # Demo: Error classification
    print("\n" + "=" * 70)
    print("ERROR CLASSIFICATION DEMO")
    print("=" * 70)

    test_errors = [
        Exception("Timeout after 10s"),
        Exception("HTTP 429: Rate limit exceeded"),
        Exception("HTTP 503 Service Unavailable"),
        Exception("HTTP 404 Not Found"),
        Exception("HTTP 401 Unauthorized"),
        Exception("Connection reset by peer"),
    ]

    print("\nError type | Classification")
    print("-" * 50)
    for error in test_errors:
        error_type = classify_error(error)
        print(f"{str(error):40} | {error_type.value.upper()}")

    # Demo: Retry decorator
    print("\n" + "=" * 70)
    print("RETRY DECORATOR DEMO")
    print("=" * 70)

    attempt_count = 0

    @with_retry("demo_operation", max_attempts=3, initial_delay=0.1, max_delay=0.5)
    def flaky_operation():
        global attempt_count
        attempt_count += 1

        if attempt_count < 3:
            raise Exception("Timeout: operation timed out")

        return "Success!"

    print("\nSimulating flaky operation that fails twice then succeeds...")
    attempt_count = 0
    try:
        result = flaky_operation()
        print(f"Result: {result} (succeeded on attempt {attempt_count}/3)")
    except Exception as e:
        print(f"Failed: {e}")

    print("\n" + "=" * 70)
    print("DEMO COMPLETE")
    print("=" * 70)
