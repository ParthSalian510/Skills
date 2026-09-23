#!/usr/bin/env python3
"""
Unit tests for webhook server.

Tests webhook signature validation, event extraction, queue management,
and endpoint behavior.

Run: python3 tests/test_webhook_server.py
"""
import sys
import json
import hmac
import hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from webhook_server import (
    WebhookValidator, WebhookQueue, WebhookHandler, WebhookConfig
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


# Test 1: Signature validation
try:
    secret = "test-secret"
    payload = b'{"test": "data"}'

    # Compute correct signature
    computed_hash = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    correct_signature = f"sha256={computed_hash}"

    # Valid signature
    check("Validates correct signature",
          WebhookValidator.validate_signature(payload, correct_signature, secret))

    # Invalid signature
    bad_signature = "sha256=invalid"
    check("Rejects invalid signature",
          not WebhookValidator.validate_signature(payload, bad_signature, secret))

    # Missing signature
    check("Rejects missing signature",
          not WebhookValidator.validate_signature(payload, "", secret))

except Exception as e:
    failed += 1
    print(f"✗ Signature validation tests failed: {e}")

# Test 2: Event extraction
try:
    valid_event = {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": "SR-4028",
            "updated": "2026-09-23T14:30:00Z",
            "fields": {
                "status": {"name": "In Progress"},
                "priority": {"name": "P1"},
                "assignee": {"displayName": "Jane Doe"},
                "summary": "Fix authentication bug",
            }
        }
    }

    data = WebhookValidator.extract_event_data(valid_event)
    check("Extracts ticket_id", data.get("ticket_id") == "SR-4028")
    check("Extracts status", data.get("status") == "In Progress")
    check("Extracts priority", data.get("priority") == "P1")
    check("Extracts assignee", data.get("assignee") == "Jane Doe")
    check("Extracts summary", data.get("summary") == "Fix authentication bug")
    check("Extracts event_type", data.get("event_type") == "jira:issue_updated")

except Exception as e:
    failed += 1
    print(f"✗ Event extraction tests failed: {e}")

# Test 3: Invalid event handling
try:
    invalid_events = [
        {},  # Empty event
        {"webhookEvent": "jira:issue_updated"},  # Missing issue
        {"issue": {}},  # Missing webhookEvent
    ]

    for invalid_event in invalid_events:
        data = WebhookValidator.extract_event_data(invalid_event)
        check("Rejects invalid event", data is None)

except Exception as e:
    failed += 1
    print(f"✗ Invalid event tests failed: {e}")

# Test 4: Queue operations
try:
    queue = WebhookQueue(max_size=5)

    # Enqueue tasks
    task1 = {"ticket_id": "SR-4028", "status": "queued"}
    task2 = {"ticket_id": "SR-4029", "status": "queued"}

    check("Enqueues task 1", queue.enqueue(task1))
    check("Enqueues task 2", queue.enqueue(task2))
    check("Queue size is 2", queue.size() == 2)

    # Dequeue tasks
    dequeued1 = queue.dequeue()
    check("Dequeues task 1", dequeued1["ticket_id"] == "SR-4028")

    dequeued2 = queue.dequeue()
    check("Dequeues task 2", dequeued2["ticket_id"] == "SR-4029")

    check("Queue size is 0", queue.size() == 0)

    # Dequeue from empty queue
    empty = queue.dequeue()
    check("Returns None when empty", empty is None)

except Exception as e:
    failed += 1
    print(f"✗ Queue tests failed: {e}")

# Test 5: Queue max size
try:
    queue = WebhookQueue(max_size=3)

    # Fill queue
    for i in range(3):
        queue.enqueue({"ticket_id": f"SR-{4000+i}"})

    check("Queue at max size", queue.size() == 3)

    # Add to full queue - should drop oldest (deque with maxlen behavior)
    queue.enqueue({"ticket_id": "SR-4003"})
    check("Queue still at max size after overflow", queue.size() == 3)

except Exception as e:
    failed += 1
    print(f"✗ Queue max size tests failed: {e}")

# Test 6: Queue statistics
try:
    queue = WebhookQueue()
    queue.enqueue({"ticket_id": "SR-4028"})
    queue.enqueue({"ticket_id": "SR-4029"})

    stats = queue.stats()
    check("Stats has queue_size", "queue_size" in stats)
    check("Stats has processed", "processed" in stats)
    check("Stats has failed", "failed" in stats)
    check("Queue size in stats", stats["queue_size"] == 2)

except Exception as e:
    failed += 1
    print(f"✗ Queue stats tests failed: {e}")

# Test 7: Webhook handler - valid event
try:
    config = WebhookConfig(secret="test-secret", debug=False)
    handler = WebhookHandler(config)

    event = {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": "SR-4028",
            "updated": "2026-09-23T14:30:00Z",
            "fields": {
                "status": {"name": "In Progress"},
                "priority": {"name": "P1"},
                "assignee": {"displayName": "Jane Doe"},
                "summary": "Fix bug",
            }
        }
    }

    payload = json.dumps(event).encode()
    signature = f"sha256={hmac.new('test-secret'.encode(), payload, hashlib.sha256).hexdigest()}"

    result = handler.handle_webhook(payload, signature)
    check("Returns success status", result["status"] == "queued")
    check("Returns ticket_id", result["ticket_id"] == "SR-4028")
    check("Queue has task", handler.queue.size() == 1)

except Exception as e:
    failed += 1
    print(f"✗ Webhook handler tests failed: {e}")

# Test 8: Webhook handler - invalid signature
try:
    config = WebhookConfig(secret="test-secret")
    handler = WebhookHandler(config)

    event = {"webhookEvent": "jira:issue_updated", "issue": {"key": "SR-4028", "fields": {}}}
    payload = json.dumps(event).encode()
    bad_signature = "sha256=invalid"

    result = handler.handle_webhook(payload, bad_signature)
    check("Rejects invalid signature", result["status"] == "error")
    check("Returns error code", result["code"] == "invalid_signature")
    check("Queue empty", handler.queue.size() == 0)

except Exception as e:
    failed += 1
    print(f"✗ Invalid signature handler tests failed: {e}")

# Test 9: Webhook handler - invalid JSON
try:
    config = WebhookConfig(secret="test-secret")
    handler = WebhookHandler(config)

    payload = b"invalid json"
    signature = f"sha256={hmac.new('test-secret'.encode(), payload, hashlib.sha256).hexdigest()}"

    result = handler.handle_webhook(payload, signature)
    check("Rejects invalid JSON", result["status"] == "error")
    check("Returns JSON error code", result["code"] == "invalid_json")

except Exception as e:
    failed += 1
    print(f"✗ Invalid JSON tests failed: {e}")

# Test 10: Webhook handler - invalid event structure
try:
    config = WebhookConfig(secret="test-secret")
    handler = WebhookHandler(config)

    event = {"webhookEvent": "jira:issue_updated"}  # Missing "issue"
    payload = json.dumps(event).encode()
    signature = f"sha256={hmac.new('test-secret'.encode(), payload, hashlib.sha256).hexdigest()}"

    result = handler.handle_webhook(payload, signature)
    check("Rejects invalid event structure", result["status"] == "error")
    check("Returns invalid_event code", result["code"] == "invalid_event")

except Exception as e:
    failed += 1
    print(f"✗ Invalid event structure tests failed: {e}")

# Test 11: Event logging
try:
    config = WebhookConfig(secret="test-secret")
    handler = WebhookHandler(config)

    event1 = {
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

    event2 = {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": "SR-4029",
            "updated": "2026-09-23T15:00:00Z",
            "fields": {
                "status": {"name": "Done"},
                "priority": {"name": "P1"},
            }
        }
    }

    for event in [event1, event2]:
        payload = json.dumps(event).encode()
        signature = f"sha256={hmac.new('test-secret'.encode(), payload, hashlib.sha256).hexdigest()}"
        handler.handle_webhook(payload, signature)

    check("Logs events", len(handler.event_log) == 2)
    check("First event logged", handler.event_log[0]["ticket_id"] == "SR-4028")
    check("Second event logged", handler.event_log[1]["ticket_id"] == "SR-4029")

except Exception as e:
    failed += 1
    print(f"✗ Event logging tests failed: {e}")

# Test 12: Status check
try:
    config = WebhookConfig(secret="test-secret")
    handler = WebhookHandler(config)

    event = {
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

    payload = json.dumps(event).encode()
    signature = f"sha256={hmac.new('test-secret'.encode(), payload, hashlib.sha256).hexdigest()}"
    handler.handle_webhook(payload, signature)

    status = handler.get_status()
    check("Status has operational status", status["status"] == "operational")
    check("Status has queue info", "queue" in status)
    check("Status has recent events", "recent_events" in status)
    check("Queue size in status", status["queue"]["queue_size"] == 1)

except Exception as e:
    failed += 1
    print(f"✗ Status check tests failed: {e}")

# Test 13: Multiple events with same ticket
try:
    config = WebhookConfig(secret="test-secret")
    handler = WebhookHandler(config)

    for status in ["Open", "In Progress", "Done"]:
        event = {
            "webhookEvent": "jira:issue_updated",
            "issue": {
                "key": "SR-4028",
                "updated": "2026-09-23T14:30:00Z",
                "fields": {
                    "status": {"name": status},
                    "priority": {"name": "P1"},
                }
            }
        }

        payload = json.dumps(event).encode()
        signature = f"sha256={hmac.new('test-secret'.encode(), payload, hashlib.sha256).hexdigest()}"
        result = handler.handle_webhook(payload, signature)
        check(f"Handles status {status}", result["status"] == "queued")

    check("Queue has 3 tasks", handler.queue.size() == 3)

except Exception as e:
    failed += 1
    print(f"✗ Multiple events tests failed: {e}")

# Summary
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
