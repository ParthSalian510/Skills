#!/usr/bin/env python3
"""
Unit tests for error messages.

Verifies that all error messages are well-formatted, actionable,
and include the required components: description, causes, and remediation.

Run: python3 tests/test_error_messages.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from error_messages import error_message, ERRORS

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


# Test 1: All error types in registry have test parameters
error_test_params = {
    "ticket_not_found": {"ticket_id": "SR-4028"},
    "customer_missing": {"ticket_id": "SR-4028"},
    "priority_invalid": {"ticket_id": "SR-4028", "priority_value": "P0"},
    "channel_already_exists": {"ticket_id": "SR-4028", "channel_name": "sr-4028-test-low"},
    "slack_search_error": {"ticket_id": "SR-4028", "error_code": "429", "error_text": "Rate limit exceeded"},
    "trigger_button_not_found": {"ticket_id": "SR-4028"},
    "iframe_not_found": {"ticket_id": "SR-4028"},
    "create_button_not_found": {"ticket_id": "SR-4028"},
    "browser_interaction_failed": {"ticket_id": "SR-4028", "action": "Click 'Create channel' button"},
    "browser_navigation_slow": {"ticket_id": "SR-4028"},
    "channel_verification_failed": {"ticket_id": "SR-4028", "channel_name": "sr-4028-test-low", "search_result": "Not found"},
    "channel_search_error": {"ticket_id": "SR-4028", "error_code": "400", "error_text": "Invalid request"},
    "message_post_error": {"ticket_id": "SR-4028", "channel_name": "sr-4028-test-low", "error_code": "403", "error_text": "Forbidden"},
    "message_post_timeout": {"ticket_id": "SR-4028", "channel_name": "sr-4028-test-low"},
}

check("All error types have test parameters", len(error_test_params) == len(ERRORS))

# Test 2: Generate and validate each error message
for error_type, params in error_test_params.items():
    try:
        msg = error_message(error_type, **params)

        # Check that message is not empty
        check(f"Error message '{error_type}' is not empty", len(msg) > 10)

        # Check that message starts with emoji or 'Step'
        check(
            f"Error message '{error_type}' starts with emoji",
            msg.strip().startswith("❌") or msg.strip().startswith("ℹ️") or msg.strip().startswith("⏱️")
        )

        # Check for "What to do:" or "Why this happened:" (action-oriented)
        has_why_or_what = ("Why this happened:" in msg or "Why" in msg) or ("What to do:" in msg or "What" in msg)
        check(
            f"Error message '{error_type}' includes causes or remediation",
            has_why_or_what
        )

        # Check that message has multiple lines (formatted, not single line)
        line_count = len(msg.strip().split("\n"))
        check(
            f"Error message '{error_type}' is multi-line",
            line_count > 3
        )

    except Exception as e:
        failed += 1
        print(f"✗ Error message '{error_type}' failed: {e}")

# Test 3: Verify error message quality characteristics
quality_checks = {
    "ticket_not_found": {
        "ticket_id": "SR-4028"
    },
    "channel_already_exists": {
        "ticket_id": "SR-4028",
        "channel_name": "sr-4028-test-low"
    },
    "trigger_button_not_found": {
        "ticket_id": "SR-4028"
    }
}

for error_type, params in quality_checks.items():
    msg = error_message(error_type, **params)

    # Check for actionable language
    has_actionable = any(word in msg.lower() for word in [
        "do:", "check", "verify", "try", "contact", "wait", "report"
    ])
    check(
        f"Error message '{error_type}' is actionable",
        has_actionable
    )

    # Check for specific context (include params in message)
    if "ticket_id" in params:
        check(
            f"Error message '{error_type}' includes ticket ID",
            params["ticket_id"] in msg
        )

    # Check for URLs or contact info if relevant
    if error_type in ["ticket_not_found", "trigger_button_not_found"]:
        has_jira_ref = "bloo-systems.atlassian.net" in msg or "Jira" in msg
        check(
            f"Error message '{error_type}' includes Jira reference",
            has_jira_ref
        )

# Test 4: Error message consistency
sample_errors = [
    error_message("ticket_not_found", ticket_id="TEST"),
    error_message("channel_already_exists", ticket_id="TEST", channel_name="test"),
    error_message("browser_interaction_failed", ticket_id="TEST", action="test"),
]

for msg in sample_errors:
    # All should have step information
    check("Error message includes step info", "Step:" in msg)

    # All should have clear emoji indicator
    has_emoji = msg.strip()[0] in "❌ℹ️⏱️"
    check("Error message starts with emoji", has_emoji)

# Test 5: Unknown error type handling
unknown_msg = error_message("nonexistent_error_type")
check("Unknown error type returns safe message", "Unknown error type" in unknown_msg)

# Summary
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
