#!/usr/bin/env python3
"""
Unit tests for audit logger.

Verifies step recording, action tracking, error formatting, JSONL output,
and performance impact.

Run: python3 tests/test_audit_logger.py
"""
import sys
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from audit_logger import (
    AuditLogger, generate_run_id,
    audit_preflight_checks, audit_jira_fetch, audit_slack_search,
    audit_browser_navigation, audit_slack_verification, audit_message_post,
    audit_error
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


# Test 1: Run ID generation
try:
    run_id = generate_run_id()
    check("Run ID has correct format", "-" in run_id and len(run_id) > 15)

    run_id2 = generate_run_id()
    check("Run IDs are unique", run_id != run_id2)

except Exception as e:
    failed += 1
    print(f"✗ Run ID generation failed: {e}")

# Test 2: AuditLogger initialization
try:
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.log.jsonl"
        logger = AuditLogger("SR-4028", "SR", "5Tattva", "P3", log_path=log_path)

        check("Logger initializes", logger is not None)
        check("Logger has run_id", len(logger.run_id) > 15)
        check("Logger tracks ticket", logger.ticket_id == "SR-4028")
        check("Logger tracks customer", logger.customer == "5Tattva")

except Exception as e:
    failed += 1
    print(f"✗ AuditLogger initialization failed: {e}")

# Test 3: Step recording
try:
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.log.jsonl"
        logger = AuditLogger("SR-4028", "SR", "5Tattva", "P3", log_path=log_path)

        logger.record_step(0, "Pre-flight Checks", "success", 2.1)
        check("Step is recorded", len(logger.steps) == 1)

        step = logger.steps[0]
        check("Step has number", step["step"] == 0)
        check("Step has name", step["name"] == "Pre-flight Checks")
        check("Step has status", step["status"] == "success")
        check("Step has duration", "duration_seconds" in step)

except Exception as e:
    failed += 1
    print(f"✗ Step recording failed: {e}")

# Test 4: Step with details
try:
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.log.jsonl"
        logger = AuditLogger("SR-4028", "SR", "5Tattva", "P3", log_path=log_path)

        details = {"key": "value", "count": 42}
        logger.record_step(1, "Fetch metadata", "success", 1.5, details=details)

        step = logger.steps[0]
        check("Step includes details", "details" in step)
        check("Details are preserved", step["details"]["key"] == "value")

except Exception as e:
    failed += 1
    print(f"✗ Step with details failed: {e}")

# Test 5: Step with error
try:
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.log.jsonl"
        logger = AuditLogger("SR-4028", "SR", "5Tattva", "P3", log_path=log_path)

        error = audit_error("ticket_not_found", "Ticket SR-9999 not found", "TNF")
        logger.record_step(1, "Fetch metadata", "failure", 0.5, error=error)

        step = logger.steps[0]
        check("Failed step includes error", "error" in step)
        check("Error has type", step["error"]["type"] == "ticket_not_found")
        check("Error has message", "not found" in step["error"]["message"])

except Exception as e:
    failed += 1
    print(f"✗ Step with error failed: {e}")

# Test 6: Action recording (sub-steps)
try:
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.log.jsonl"
        logger = AuditLogger("SR-4028", "SR", "5Tattva", "P3", log_path=log_path)

        logger.record_action(4, "navigate", 8.0, {"url": "https://example.com"})
        logger.record_action(4, "click_button", 1.0, {"button": "Create"})
        logger.record_step(4, "Browser automation", "success", 9.0)

        step = logger.steps[0]
        check("Step includes actions", "actions" in step)
        check("Actions are recorded", len(step["actions"]) == 2)
        check("First action is correct", step["actions"][0]["action"] == "navigate")

except Exception as e:
    failed += 1
    print(f"✗ Action recording failed: {e}")

# Test 7: Log finalization and file writing
try:
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.log.jsonl"
        logger = AuditLogger("SR-4028", "SR", "5Tattva", "P3", log_path=log_path)

        logger.record_step(0, "Pre-flight", "success", 1.0)
        logger.record_step(1, "Fetch", "success", 2.0)
        logger.finalize("sr-4028-5tattva-low", "success")

        check("Log file was created", log_path.exists())

        # Read and parse the file
        with open(log_path) as f:
            content = f.read().strip()

        entry = json.loads(content)
        check("Log entry is valid JSON", entry is not None)
        check("Log entry has run_id", "run_id" in entry)
        check("Log entry has steps", "steps" in entry)
        check("Log entry has summary", "summary" in entry)

except Exception as e:
    failed += 1
    print(f"✗ Log finalization failed: {e}")

# Test 8: JSONL format (one JSON per line)
try:
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.log.jsonl"

        # Create two log entries
        logger1 = AuditLogger("SR-4028", "SR", "5Tattva", "P3", log_path=log_path)
        logger1.record_step(0, "Test1", "success", 1.0)
        logger1.finalize("channel1", "success")

        logger2 = AuditLogger("SR-4029", "SR", "CanFin", "P3", log_path=log_path)
        logger2.record_step(0, "Test2", "success", 1.0)
        logger2.finalize("channel2", "success")

        # Verify JSONL format (one JSON per line)
        with open(log_path) as f:
            lines = [line.strip() for line in f if line.strip()]

        check("JSONL has 2 entries", len(lines) == 2)

        # Verify each line is valid JSON
        entries = []
        for line in lines:
            entry = json.loads(line)
            entries.append(entry)

        check("All entries are valid JSON", len(entries) == 2)
        check("First entry is SR-4028", entries[0]["ticket_id"] == "SR-4028")
        check("Second entry is SR-4029", entries[1]["ticket_id"] == "SR-4029")

except Exception as e:
    failed += 1
    print(f"✗ JSONL format test failed: {e}")

# Test 9: Summary statistics
try:
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.log.jsonl"
        logger = AuditLogger("SR-4028", "SR", "5Tattva", "P3", log_path=log_path)

        logger.record_step(0, "Step 0", "success", 1.0)
        logger.record_step(1, "Step 1", "success", 1.0)
        logger.record_step(2, "Step 2", "failure", 0.5)
        logger.finalize("channel", "failure")

        # Read the log entry
        with open(log_path) as f:
            entry = json.loads(f.read())

        summary = entry["summary"]
        check("Summary has total_steps", summary["total_steps"] == 3)
        check("Summary counts successes", summary["steps_succeeded"] == 2)
        check("Summary counts failures", summary["steps_failed"] == 1)
        check("Summary has final_status", summary["final_status"] == "failure")

except Exception as e:
    failed += 1
    print(f"✗ Summary statistics test failed: {e}")

# Test 10: Helper functions
try:
    # Test preflight checks helper
    checks = audit_preflight_checks("connected", "ok", "ok", 2.1)
    check("Preflight checks helper works", "checks" in checks)

    # Test Jira fetch helper
    jira = audit_jira_fetch("SR-4028", "5Tattva", "P3")
    check("Jira fetch helper works", "details" in jira)

    # Test Slack search helper
    search = audit_slack_search(False, "SR-4028")
    check("Slack search helper works", "details" in search)

    # Test error helper
    error = audit_error("test_error", "Test message", "TST")
    check("Error helper includes type", error["type"] == "test_error")
    check("Error helper includes code", error["code"] == "TST")

except Exception as e:
    failed += 1
    print(f"✗ Helper functions test failed: {e}")

# Test 11: No performance regression
try:
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.log.jsonl"

        import time

        # Time 100 log entries
        start = time.time()
        for i in range(100):
            logger = AuditLogger(f"SR-{i}", "SR", "Test", "P3", log_path=log_path)
            logger.record_step(0, "Test", "success", 1.0)
            logger.finalize(f"channel-{i}", "success")
        elapsed = time.time() - start

        # Should complete in reasonable time (< 5 seconds for 100 entries)
        check(f"100 log entries in {elapsed:.2f}s", elapsed < 5.0)

        # Check file size
        file_size = log_path.stat().st_size
        avg_entry_size = file_size / 100
        check(f"Average entry size {avg_entry_size:.0f} bytes", avg_entry_size > 100 and avg_entry_size < 5000)

except Exception as e:
    failed += 1
    print(f"✗ Performance test failed: {e}")

# Summary
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
