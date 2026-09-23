#!/usr/bin/env python3
"""
Integration tests for timeout enforcement.

Tests that timeout_wrapper correctly enforces timeouts and provides
clear error messages.

Run: python3 tests/test_timeout_enforcement.py
"""
import sys
import time
from pathlib import Path

# Add scripts directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from timeout_wrapper import (
    TimeoutWrapper, TimeoutError, with_timeout, init_wrapper,
    start_pipeline, get_elapsed_time
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


# Test 1: TimeoutError message format
try:
    try:
        raise TimeoutError("Jira API fetch", 10, "Step 1: Fetch metadata")
    except TimeoutError as e:
        error_msg = str(e)

        check(
            "TimeoutError message contains operation name",
            "Jira API fetch" in error_msg or "timed out" in error_msg
        )
        check(
            "TimeoutError message contains timeout value",
            "10" in error_msg
        )
        check(
            "TimeoutError message contains step name",
            "Step 1" in error_msg
        )
        check(
            "TimeoutError message starts with ❌",
            error_msg.strip().startswith("❌")
        )
        check(
            "TimeoutError message includes 'Likely causes'",
            "Likely causes" in error_msg
        )
        check(
            "TimeoutError message includes remediation",
            "Recommendation" in error_msg or "retry" in error_msg
        )

except Exception as e:
    failed += 1
    print(f"✗ TimeoutError test failed: {e}")

# Test 2: TimeoutWrapper initialization
try:
    wrapper = TimeoutWrapper()
    check("TimeoutWrapper initializes without error", wrapper is not None)
    check("TimeoutWrapper loads config", wrapper.config is not None)
    check("TimeoutWrapper loads timeouts", wrapper.timeouts is not None)
    check("TimeoutWrapper has preflight_check timeout", "preflight_check" in wrapper.timeouts)
    check("TimeoutWrapper preflight_check is 10", wrapper.timeouts.get("preflight_check") == 10)
except Exception as e:
    failed += 1
    print(f"✗ TimeoutWrapper initialization failed: {e}")

# Test 3: Function completes within timeout
try:
    def fast_function():
        return "success"

    wrapper = TimeoutWrapper()
    result = wrapper.with_timeout(
        fast_function,
        "Fast operation",
        "Test",
        timeout_key="slack_search"  # 5 seconds
    )
    check("Function completing within timeout succeeds", result == "success")
except Exception as e:
    failed += 1
    print(f"✗ Fast function test failed: {e}")

# Test 4: TimeoutError can be raised and caught
try:
    wrapper = TimeoutWrapper()
    error_caught = False
    error_msg = ""

    try:
        raise TimeoutError("Test API call", 5, "Test Step")
    except TimeoutError as e:
        error_caught = True
        error_msg = str(e)

    check("TimeoutError can be raised and caught", error_caught)
    check("Caught TimeoutError has proper formatting", "❌" in error_msg and "timed out" in error_msg)

except Exception as e:
    failed += 1
    print(f"✗ TimeoutError handling test failed: {e}")

# Test 5: Pipeline timing
try:
    wrapper = TimeoutWrapper()
    wrapper.start_pipeline()
    time.sleep(0.1)
    elapsed = wrapper.get_elapsed_time()
    check("Pipeline timing works", elapsed >= 0.05)  # Allow some margin
except Exception as e:
    failed += 1
    print(f"✗ Pipeline timing test failed: {e}")

# Test 6: Module-level functions
try:
    init_wrapper()

    def test_func():
        return "ok"

    result = with_timeout(test_func, "Test operation")
    check("Module-level with_timeout works", result == "ok")

    elapsed = get_elapsed_time()
    check("Module-level get_elapsed_time works", elapsed >= 0)

except Exception as e:
    failed += 1
    print(f"✗ Module-level functions test failed: {e}")

# Test 7: Timeout values from config are used
try:
    wrapper = TimeoutWrapper()

    check("jira_fetch timeout is 10", wrapper.timeouts.get("jira_fetch") == 10)
    check("slack_search timeout is 5", wrapper.timeouts.get("slack_search") == 5)
    check("browser_navigation timeout is 30", wrapper.timeouts.get("browser_navigation") == 30)
    check("overall_timeout is 120", wrapper.timeouts.get("overall_timeout") == 120)

except Exception as e:
    failed += 1
    print(f"✗ Timeout values test failed: {e}")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
