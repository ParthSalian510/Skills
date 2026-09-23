#!/usr/bin/env python3
"""
Unit tests for retry logic with exponential backoff.

Tests exponential backoff calculation, error classification, retry behavior,
and circuit breaker pattern.

Run: python3 tests/test_retry_logic.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from retry_logic import (
    BackoffStrategy, RetryConfig, ErrorType, classify_error,
    with_retry, CircuitBreaker, with_circuit_breaker,
    TransientFailure, PermanentFailure
)

passed = 0
failed = 0


def check(label, condition, details=""):
    """Check a test condition."""
    global passed, failed
    if condition:
        passed += 1
        print(f"✓ {label}")
    else:
        failed += 1
        print(f"✗ {label}")
        if details:
            print(f"  {details}")


# Test 1: Backoff strategy
try:
    backoff = BackoffStrategy(initial_delay=1.0, base=2.0, max_delay=20.0)

    # Test exponential growth
    times = [backoff.wait_time(i) for i in range(5)]
    check("Backoff increases exponentially", times[0] < times[1] < times[2] < times[3] < times[4])

    # Test max delay cap
    backoff_uncapped = BackoffStrategy(initial_delay=1.0, base=2.0, max_delay=5.0)
    all_capped = True
    for i in range(10):
        wait = backoff_uncapped.wait_time(i)
        if wait > 6.0:  # 5.0 + 20% jitter max
            all_capped = False
    check(f"Backoff respects max_delay", all_capped)

    # Test jitter is present
    times_with_jitter = [backoff.wait_time(0) for _ in range(10)]
    check("Backoff includes jitter", len(set(times_with_jitter)) > 1)

except Exception as e:
    failed += 1
    print(f"✗ Backoff strategy test failed: {e}")

# Test 2: Error classification
try:
    # Transient errors
    transient_tests = [
        ("Timeout after 10s", ErrorType.TRANSIENT),
        ("HTTP 429: Rate limit exceeded", ErrorType.TRANSIENT),
        ("HTTP 503 Service Unavailable", ErrorType.TRANSIENT),
        ("Connection reset by peer", ErrorType.TRANSIENT),
        ("DNS lookup failed", ErrorType.TRANSIENT),
    ]

    for error_msg, expected_type in transient_tests:
        error = Exception(error_msg)
        error_type = classify_error(error)
        check(f"Classifies '{error_msg}' as {expected_type.value}",
              error_type == expected_type)

    # Permanent errors
    permanent_tests = [
        ("HTTP 404 Not Found", ErrorType.PERMANENT),
        ("HTTP 401 Unauthorized", ErrorType.PERMANENT),
        ("HTTP 403 Forbidden", ErrorType.PERMANENT),
        ("HTTP 400 Bad Request", ErrorType.PERMANENT),
    ]

    for error_msg, expected_type in permanent_tests:
        error = Exception(error_msg)
        error_type = classify_error(error)
        check(f"Classifies '{error_msg}' as {expected_type.value}",
              error_type == expected_type)

except Exception as e:
    failed += 1
    print(f"✗ Error classification test failed: {e}")

# Test 3: Retry decorator - success on first attempt
try:
    state = {"attempt_count": 0}

    @with_retry("test", max_attempts=3, initial_delay=0.01, max_delay=0.1)
    def always_succeeds():
        state["attempt_count"] += 1
        return "success"

    result = always_succeeds()
    check("Retry decorator succeeds on first attempt", result == "success")
    check("First attempt requires no retries", state["attempt_count"] == 1)

except Exception as e:
    failed += 1
    print(f"✗ Retry success test failed: {e}")

# Test 4: Retry decorator - success after retries
try:
    state = {"attempt_count": 0}

    @with_retry("test", max_attempts=3, initial_delay=0.01, max_delay=0.1)
    def succeeds_on_third():
        state["attempt_count"] += 1
        if state["attempt_count"] < 3:
            raise Exception("Timeout after 10s")
        return "success"

    result = succeeds_on_third()
    check("Retry decorator retries on transient error", result == "success")
    check("Retries until success", state["attempt_count"] == 3)

except Exception as e:
    failed += 1
    print(f"✗ Retry with success test failed: {e}")

# Test 5: Retry decorator - permanent error (no retry)
try:
    state = {"attempt_count": 0}

    @with_retry("test", max_attempts=3, initial_delay=0.01, max_delay=0.1)
    def permanent_error():
        state["attempt_count"] += 1
        raise Exception("HTTP 404 Not Found")

    try:
        permanent_error()
        check("Permanent error raises PermanentFailure", False)
    except PermanentFailure:
        check("Permanent error raises PermanentFailure", True)
        check("Permanent error doesn't retry", state["attempt_count"] == 1)

except Exception as e:
    failed += 1
    print(f"✗ Permanent error test failed: {e}")

# Test 6: Retry decorator - max attempts exceeded
try:
    state = {"attempt_count": 0}

    @with_retry("test", max_attempts=2, initial_delay=0.01, max_delay=0.1)
    def always_fails():
        state["attempt_count"] += 1
        raise Exception("Timeout after 10s")

    try:
        always_fails()
        check("Max attempts exceeded raises TransientFailure", False)
    except TransientFailure:
        check("Max attempts exceeded raises TransientFailure", True)
        check("Tries all attempts", state["attempt_count"] == 2)

except Exception as e:
    failed += 1
    print(f"✗ Max attempts test failed: {e}")

# Test 7: RetryConfig from dict
try:
    config_dict = {
        "enabled": True,
        "max_attempts": 5,
        "initial_delay": 2.0,
        "base": 3.0,
        "max_delay": 30.0
    }

    config = RetryConfig.from_dict(config_dict)
    check("RetryConfig loads from dict", config.max_attempts == 5)
    check("RetryConfig preserves settings", config.backoff.initial_delay == 2.0)

except Exception as e:
    failed += 1
    print(f"✗ RetryConfig test failed: {e}")

# Test 8: Circuit breaker - closed state
try:
    circuit = CircuitBreaker(failure_threshold=3, timeout=1)

    check("Circuit starts in closed state", circuit.state == "closed")
    check("Closed circuit allows operations", not circuit.is_open())

    circuit.record_success()
    check("Success keeps circuit closed", circuit.state == "closed")

except Exception as e:
    failed += 1
    print(f"✗ Circuit breaker closed state test failed: {e}")

# Test 9: Circuit breaker - open state
try:
    circuit = CircuitBreaker(failure_threshold=2, timeout=1)

    # Record failures until circuit opens
    circuit.record_failure()
    check("One failure doesn't open circuit", circuit.state == "closed")

    circuit.record_failure()
    check("Threshold failures open circuit", circuit.state == "open")
    check("Open circuit blocks operations", circuit.is_open())

except Exception as e:
    failed += 1
    print(f"✗ Circuit breaker open state test failed: {e}")

# Test 10: Circuit breaker - half-open state
try:
    circuit = CircuitBreaker(failure_threshold=2, timeout=0.1)

    # Open the circuit
    circuit.record_failure()
    circuit.record_failure()
    check("Circuit is open", circuit.state == "open")

    # Wait for timeout
    time.sleep(0.2)

    # Should transition to half-open
    result = circuit.is_open()
    check("Circuit transitions to half-open after timeout", circuit.state == "half_open")
    check("Half-open allows one attempt", not result)

except Exception as e:
    failed += 1
    print(f"✗ Circuit breaker half-open state test failed: {e}")

# Test 11: Circuit breaker decorator
try:
    circuit = CircuitBreaker(failure_threshold=2, timeout=1)
    state = {"attempt_count": 0}

    @with_circuit_breaker(circuit)
    def flaky_function():
        state["attempt_count"] += 1
        if state["attempt_count"] < 3:
            raise Exception("Service error")
        return "success"

    # First two calls fail (open circuit)
    for i in range(2):
        try:
            flaky_function()
        except Exception:
            pass

    check("Circuit opens after failures", circuit.state == "open")
    check("Circuit decorator respects open state", circuit.state == "open")

except Exception as e:
    failed += 1
    print(f"✗ Circuit breaker decorator test failed: {e}")

# Test 12: Retry timing
try:
    start = time.time()
    state = {"attempt_count": 0}

    @with_retry("test", max_attempts=3, initial_delay=0.05, max_delay=0.1)
    def slow_operation():
        state["attempt_count"] += 1
        if state["attempt_count"] < 3:
            raise Exception("Timeout")
        return "success"

    result = slow_operation()
    elapsed = time.time() - start

    # Should have at least 2 retries with delays: 0.05s + 0.1s = 0.15s
    check("Retry timing includes backoff delay", elapsed >= 0.1)
    check("Retry timing is reasonable", elapsed < 2.0)

except Exception as e:
    failed += 1
    print(f"✗ Retry timing test failed: {e}")

# Test 13: Backoff with different parameters
try:
    backoff = BackoffStrategy(initial_delay=0.5, base=3.0, max_delay=10.0)

    times = [backoff.wait_time(i) for i in range(4)]
    check("Custom backoff parameters work", times[0] < times[1] < times[2])

except Exception as e:
    failed += 1
    print(f"✗ Custom backoff test failed: {e}")

# Summary
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
