#!/usr/bin/env python3
"""
Unit tests for Tier 2: Ticket-to-Channel Sync

Tests state change detection, channel update formatting, and sync logic.

Run: python3 tests/test_ticket_sync.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from ticket_sync import (
    TicketSyncManager, StateChangeDetector, WebhookValidator,
    TicketState
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


# Test 1: State change detection
try:
    old_state = {
        "ticket_id": "SR-4028",
        "status": "Open",
        "priority": "P3",
        "assignee": "John",
        "summary": "Fix bug",
    }

    new_state = {
        "ticket_id": "SR-4028",
        "status": "In Progress",
        "priority": "P3",
        "assignee": "John",
        "summary": "Fix bug",
    }

    changes = StateChangeDetector.detect_changes(old_state, new_state)
    check("Detects status change", "status" in changes)
    check("Ignores unchanged fields", "assignee" not in changes)
    check("Shows old and new values", changes["status"]["old"] == "Open")

except Exception as e:
    failed += 1
    print(f"✗ State change detection failed: {e}")

# Test 2: Status emoji
try:
    test_cases = [
        ("Open", "🟠"),
        ("In Progress", "🔵"),
        ("Done", "🟢"),
        ("Unknown", "⚪"),
    ]

    for status, expected_emoji in test_cases:
        emoji = StateChangeDetector.format_status_emoji(status)
        check(f"Status emoji for '{status}'", emoji == expected_emoji)

except Exception as e:
    failed += 1
    print(f"✗ Status emoji test failed: {e}")

# Test 3: Priority emoji
try:
    test_cases = [
        ("P1", "🔴"),
        ("P2", "🟠"),
        ("P3", "🟡"),
        ("P4", "🟢"),
    ]

    for priority, expected_emoji in test_cases:
        emoji = StateChangeDetector.format_priority_emoji(priority)
        check(f"Priority emoji for '{priority}'", emoji == expected_emoji)

except Exception as e:
    failed += 1
    print(f"✗ Priority emoji test failed: {e}")

# Test 4: Topic formatting
try:
    topic = StateChangeDetector.format_topic("SR-4028", "In Progress", "P1")
    check("Topic includes ticket ID", "SR-4028" in topic)
    check("Topic includes status", "In Progress" in topic)
    check("Topic includes priority", "P1" in topic)
    check("Topic includes emojis", "🔵" in topic and "🔴" in topic)

except Exception as e:
    failed += 1
    print(f"✗ Topic formatting test failed: {e}")

# Test 5: Pinned message formatting
try:
    state = {
        "ticket_id": "SR-4028",
        "status": "In Progress",
        "priority": "P1",
        "assignee": "Jane Doe",
        "updated_at": "2026-09-23T14:30:00Z",
    }

    message = StateChangeDetector.format_pinned_message("SR-4028", state, "https://example.com/SR-4028")
    check("Message includes ticket ID", "SR-4028" in message)
    check("Message includes status", "In Progress" in message)
    check("Message includes priority", "P1" in message)
    check("Message includes assignee", "Jane Doe" in message)
    check("Message includes Jira link", "View in Jira" in message)

except Exception as e:
    failed += 1
    print(f"✗ Pinned message test failed: {e}")

# Test 6: Archive detection
try:
    archive_statuses = ["Done", "Resolved", "Closed"]

    check("Archives 'Done' status", StateChangeDetector.should_archive("Done", archive_statuses))
    check("Archives 'Resolved' status", StateChangeDetector.should_archive("Resolved", archive_statuses))
    check("Does not archive 'Open'", not StateChangeDetector.should_archive("Open", archive_statuses))

except Exception as e:
    failed += 1
    print(f"✗ Archive detection test failed: {e}")

# Test 7: Webhook validation
try:
    valid_event = {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": "SR-4028",
            "updated": "2026-09-23T14:30:00Z",
            "fields": {
                "status": {"name": "In Progress"},
                "priority": {"name": "P1"},
            }
        }
    }

    check("Validates valid webhook", WebhookValidator.validate_signature(valid_event))

    invalid_event = {"some": "data"}
    check("Rejects invalid webhook", not WebhookValidator.validate_signature(invalid_event))

except Exception as e:
    failed += 1
    print(f"✗ Webhook validation test failed: {e}")

# Test 8: Extract ticket info from webhook
try:
    event = {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": "SR-4028",
            "updated": "2026-09-23T14:30:00Z",
            "fields": {
                "status": {"name": "In Progress"},
                "priority": {"name": "P1"},
                "assignee": {"displayName": "John Doe"},
                "summary": "Fix authentication bug",
            }
        }
    }

    info = WebhookValidator.extract_ticket_info(event)
    check("Extracts ticket ID", info.get("ticket_id") == "SR-4028")
    check("Extracts status", info.get("status") == "In Progress")
    check("Extracts priority", info.get("priority") == "P1")
    check("Extracts assignee", info.get("assignee") == "John Doe")

except Exception as e:
    failed += 1
    print(f"✗ Webhook extraction test failed: {e}")

# Test 9: Ticket sync manager - no changes
try:
    manager = TicketSyncManager()

    state = {
        "ticket_id": "SR-4028",
        "status": "Open",
        "priority": "P3",
        "assignee": "John",
        "summary": "Fix bug",
        "updated_at": "2026-09-23T10:00:00Z",
    }

    result = manager.sync_ticket("SR-4028", "sr-4028-test-low", state, "http://jira.test/SR-4028")
    check("First sync is success", result["status"] == "success")
    check("Changes detected on first sync", result["changes_detected"])

    # Second sync with same state
    result2 = manager.sync_ticket("SR-4028", "sr-4028-test-low", state, "http://jira.test/SR-4028")
    check("Second sync detects no changes", result2["status"] == "no_changes")
    check("No changes flag set", not result2["changes_detected"])

except Exception as e:
    failed += 1
    print(f"✗ Sync manager test failed: {e}")

# Test 10: Sync with status change
try:
    manager = TicketSyncManager()

    state_v1 = {
        "ticket_id": "SR-4028",
        "status": "Open",
        "priority": "P3",
        "assignee": "John",
        "summary": "Fix bug",
        "updated_at": "2026-09-23T10:00:00Z",
    }

    manager.sync_ticket("SR-4028", "sr-4028-test-low", state_v1, "http://jira.test/SR-4028")

    state_v2 = {**state_v1, "status": "In Progress", "updated_at": "2026-09-23T14:00:00Z"}
    result = manager.sync_ticket("SR-4028", "sr-4028-test-low", state_v2, "http://jira.test/SR-4028")

    check("Detects status change", "status" in result["changes"])
    check("Takes update_topic action", any(a["action"] == "update_topic" for a in result["actions_taken"]))
    check("Takes pinned message action", any(a["action"] == "update_pinned_message" for a in result["actions_taken"]))

except Exception as e:
    failed += 1
    print(f"✗ Status change sync test failed: {e}")

# Test 11: Sync with archive
try:
    manager = TicketSyncManager()

    state_v1 = {
        "ticket_id": "SR-4028",
        "status": "In Progress",
        "priority": "P1",
        "assignee": "John",
        "summary": "Fix bug",
        "updated_at": "2026-09-23T10:00:00Z",
    }

    manager.sync_ticket("SR-4028", "sr-4028-test-low", state_v1, "http://jira.test/SR-4028")

    state_v2 = {**state_v1, "status": "Done", "updated_at": "2026-09-23T17:00:00Z"}
    result = manager.sync_ticket("SR-4028", "sr-4028-test-low", state_v2, "http://jira.test/SR-4028")

    check("Archives on Done status", any(a["action"] == "archive_channel" for a in result["actions_taken"]))

except Exception as e:
    failed += 1
    print(f"✗ Archive sync test failed: {e}")

# Test 12: Sync statistics
try:
    manager = TicketSyncManager()

    state = {
        "ticket_id": "SR-4028",
        "status": "Open",
        "priority": "P3",
        "assignee": "John",
        "summary": "Fix bug",
        "updated_at": "2026-09-23T10:00:00Z",
    }

    manager.sync_ticket("SR-4028", "sr-4028-test-low", state, "http://jira.test/SR-4028")
    manager.sync_ticket("SR-4028", "sr-4028-test-low", state, "http://jira.test/SR-4028")

    stats = manager.get_sync_stats()
    check("Counts total syncs", stats["total_syncs"] == 2)
    check("Counts successful syncs", stats["successful"] == 1)
    check("Counts no-change syncs", stats["no_changes"] == 1)
    check("Calculates average duration", stats["average_duration"] >= 0)

except Exception as e:
    failed += 1
    print(f"✗ Sync stats test failed: {e}")

# Summary
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
