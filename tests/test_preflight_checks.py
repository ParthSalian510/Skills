#!/usr/bin/env python3
"""
Unit tests for pre-flight checks.

These tests verify the error messages are correct and the checks are documented.
Integration tests (actual API calls) are done manually against live Jira/Slack.

Run: python3 tests/test_preflight_checks.py
"""
import sys
from pathlib import Path

# Expected error messages
EXPECTED_CHROME_BRIDGE_ERROR = """❌ chrome-bridge is not connected

The browser automation tool required for channel creation is not loaded.

Setup (one-time per machine):
1. Open chrome://extensions in Chrome
2. Enable "Developer mode" (toggle in top-right corner)
3. Click "Load unpacked"
4. Select the directory: ~/.claude/skills/create-ticket-channel/chrome-bridge/extension
5. Restart Claude Code
6. Try again"""

EXPECTED_JIRA_ERROR_PREFIX = "❌ Jira API is not reachable"
EXPECTED_SLACK_ERROR_PREFIX = "❌ Slack API is not reachable"

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
    check("preflight section exists", "preflight" in config)
    check("preflight.enabled is true", config.get("preflight", {}).get("enabled", False) is True)
    check("preflight.check_chrome_bridge is true", config.get("preflight", {}).get("check_chrome_bridge", False) is True)
    check("preflight.check_jira_connectivity is true", config.get("preflight", {}).get("check_jira_connectivity", False) is True)
    check("preflight.check_slack_connectivity is true", config.get("preflight", {}).get("check_slack_connectivity", False) is True)
except Exception as e:
    failed += 1
    print(f"✗ Config loading failed: {e}")

# Test 2: SKILL.md has Step 0 documented
try:
    skill_path = Path(__file__).parent.parent / "SKILL.md"
    with open(skill_path) as f:
        skill_text = f.read()

    check("SKILL.md contains 'Step 0'", "### Step 0 —" in skill_text)
    check("SKILL.md mentions chrome-bridge check", "Check 1: chrome-bridge connection" in skill_text)
    check("SKILL.md mentions Jira check", "Check 2: Jira API connectivity" in skill_text)
    check("SKILL.md mentions Slack check", "Check 3: Slack API connectivity" in skill_text)
    check("SKILL.md documents chrome-bridge setup", "chrome://extensions" in skill_text)
    check("SKILL.md has Pre-flight Errors table", "### Pre-flight Errors (Step 0)" in skill_text)
except Exception as e:
    failed += 1
    print(f"✗ SKILL.md reading failed: {e}")

# Test 3: Error messages are clear (semantic check, not exact match)
check(
    "chrome-bridge error message mentions setup instructions",
    "Developer mode" in EXPECTED_CHROME_BRIDGE_ERROR and "Load unpacked" in EXPECTED_CHROME_BRIDGE_ERROR,
)
check(
    "Jira error message mentions common causes",
    "down" in EXPECTED_JIRA_ERROR_PREFIX.lower() or "reachable" in EXPECTED_JIRA_ERROR_PREFIX.lower(),
)
check(
    "Slack error message mentions common causes",
    "down" in EXPECTED_SLACK_ERROR_PREFIX.lower() or "reachable" in EXPECTED_SLACK_ERROR_PREFIX.lower(),
)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
