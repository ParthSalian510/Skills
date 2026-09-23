#!/usr/bin/env python3
"""
Unit tests for timeout configuration.

These tests verify the timeout values are correctly configured in config.yaml
and that SKILL.md documents the timeout handling strategy.

Run: python3 tests/test_timeout_config.py
"""
import sys
from pathlib import Path

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


# Test 1: Configuration loads correctly
try:
    import yaml
    config_path = Path(__file__).parent.parent / "config" / "config.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)

    check("config.yaml loads without error", config is not None)
    check("timeouts section exists", "timeouts" in config)

    timeouts = config.get("timeouts", {})

    # Check each timeout value
    check("preflight_check timeout is 10", timeouts.get("preflight_check") == 10)
    check("jira_fetch timeout is 10", timeouts.get("jira_fetch") == 10)
    check("slack_search timeout is 5", timeouts.get("slack_search") == 5)
    check("browser_navigation timeout is 30", timeouts.get("browser_navigation") == 30)
    check("browser_interaction timeout is 15", timeouts.get("browser_interaction") == 15)
    check("browser_wait_network_idle timeout is 20", timeouts.get("browser_wait_network_idle") == 20)
    check("slack_verification timeout is 10", timeouts.get("slack_verification") == 10)
    check("slack_post_message timeout is 10", timeouts.get("slack_post_message") == 10)
    check("overall_timeout is 120", timeouts.get("overall_timeout") == 120)

except Exception as e:
    failed += 1
    print(f"✗ Config loading failed: {e}")

# Test 2: SKILL.md documents timeout handling
try:
    skill_path = Path(__file__).parent.parent / "SKILL.md"
    with open(skill_path) as f:
        skill_text = f.read()

    check("SKILL.md contains 'Timeout Handling' section", "## Timeout Handling" in skill_text)
    check("SKILL.md documents timeout strategy", "Per-operation timeouts" in skill_text)
    check("SKILL.md explains 'no retry on timeout'", "No retry on timeout" in skill_text)
    check("SKILL.md documents overall timeout", "overall pipeline timeout" in skill_text)
    check("SKILL.md shows timeout error format", "⏱️ Operation timed out" in skill_text or "timeout error" in skill_text)

except Exception as e:
    failed += 1
    print(f"✗ SKILL.md reading failed: {e}")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
