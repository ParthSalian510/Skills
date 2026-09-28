#!/usr/bin/env python3
"""
Unit tests for webhook server.

Tests webhook signature validation, event extraction, queue management,
and endpoint behavior.

Run: python3 tests/test_webhook_server.py
"""
import sys
import os
os.environ.setdefault("CTC_RUN_MODE", "test")
import json
import time
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

    # Full queue rejects the new task instead of silently dropping the oldest
    check("Full queue rejects new task", queue.enqueue({"ticket_id": "SR-4003"}) is False)
    check("Queue still at max size after overflow", queue.size() == 3)
    check("Oldest task kept", queue.dequeue()["ticket_id"] == "SR-4000")

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

# Standard channel naming, secret enforcement and run logs
import tempfile
import webhook_server as ws


def read_log(log_dir):
    path = Path(log_dir) / "tests.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


try:
    ticket = {"ticket_id": "CASE-4009", "project_key": "CASE", "customer": "ISOC", "priority": "P3"}
    name, errors = ws.channel_name_for(ticket)
    check("Standard name for P3", name == "case-4009-isoc-low", f"got {name} {errors}")
    name, _ = ws.channel_name_for({**ticket, "priority": "P2"})
    check("Standard name for P2", name == "case-4009-isoc-med", f"got {name}")
    name, errors = ws.channel_name_for({**ticket, "customer": None})
    check("Missing customer is an error, not a guess", name is None and bool(errors))

    data = WebhookValidator.extract_event_data({"webhookEvent": "jira:issue_created", "issue": {
        "key": "CASE-4009", "fields": {"customfield_10002": [{"name": "ISOC"}, {"name": "Other"}],
                                        "project": {"key": "CASE"}}}})
    check("Customer is first organisation", data["customer"] == "ISOC")
    check("Missing priority stays missing", data["priority"] is None)
except Exception as e:
    failed += 1
    print(f"✗ Naming tests failed: {e}")

try:
    saved = os.environ.pop("JIRA_WEBHOOK_SECRET", None)
    try:
        ws.config_from_env()
        check("Refuses to start without secret", False)
    except RuntimeError as e:
        check("Refuses to start without secret", "JIRA_WEBHOOK_SECRET" in str(e))
    check("Explicit secret accepted", ws.config_from_env("s3cret").secret == "s3cret")
    check("No module-level app with a default secret", not hasattr(ws, "app"))
    if saved is not None:
        os.environ["JIRA_WEBHOOK_SECRET"] = saved
except Exception as e:
    failed += 1
    print(f"✗ Secret tests failed: {e}")


class FakeMessenger:
    def __init__(self, exists=False):
        self.exists, self.calls = exists, []

    def create_channel(self, name):
        self.calls.append(("create", name))
        if self.exists:
            return {"success": True, "channel_id": None, "exists": True}
        return {"success": True, "channel_id": "C123"}

    def invite_users(self, channel_id, user_ids):
        self.calls.append(("invite", channel_id))
        return True

    def send_message(self, channel_id, text, blocks=None):
        self.calls.append(("send", channel_id))
        return True

    def set_topic(self, channel_id, topic):
        self.calls.append(("topic", channel_id))
        return True

    def archive_channel(self, channel_id):
        self.calls.append(("archive", channel_id))
        return True


def jira_issue(key="CASE-4009", status="Pending", priority="P3"):
    return {"key": key, "self": "https://bloo-systems.atlassian.net/rest/api/2/issue/1", "fields": {
        "summary": "ISOC | High memory", "status": {"name": status}, "priority": {"name": priority},
        "project": {"key": "CASE"}, "customfield_10002": [{"name": "ISOC"}],
        "created": "2020-01-01T00:00:00.000+0000"}}


def event_task(event_type, **kw):
    issue = jira_issue(**kw)
    data = WebhookValidator.extract_event_data({"webhookEvent": event_type, "issue": issue})
    return {"ticket_id": issue["key"], "event_type": event_type, "ticket_data": data, "issue": issue}


real_messenger = ws.SlackMessenger
try:
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["CTC_LOG_DIR"] = tmp

        os.environ["CTC_STATE_PATH"] = str(Path(tmp) / "exists.json")
        fake = FakeMessenger(exists=True)
        ws.SlackMessenger = lambda token: fake
        ws.create_sync_callback(WebhookConfig(secret="s", slack_token="xoxb-test"))(event_task("jira:issue_created"))
        entry = read_log(tmp)[-1]
        check("Existing channel: no invite or message", [c[0] for c in fake.calls] == ["create"])
        check("Existing channel logged as skipped", entry["summary"]["final_status"] == "skipped")
        check("Skip reason recorded", entry["steps"][-1]["details"]["reason"] == "channel_already_exists")
        check("Record carries source and mode", entry["source"] == "webhook" and entry["mode"] == "test")

        os.environ["CTC_STATE_PATH"] = str(Path(tmp) / "new.json")
        fake = FakeMessenger()
        ws.SlackMessenger = lambda token: fake
        cb = ws.create_sync_callback(WebhookConfig(secret="s", slack_token="xoxb-test", slack_invite_user_ids=("U1",)))
        cb(event_task("jira:issue_created"))
        entry = read_log(tmp)[-1]
        check("New channel uses standard name", fake.calls[0] == ("create", "case-4009-isoc-low"))
        check("Success run logs 4 steps", [s["name"] for s in entry["steps"]] == [
            "Generate channel name", "Create channel", "Invite members", "Post starter message"])
        check("Success run final status", entry["summary"]["final_status"] == "success")

        fake.calls.clear()
        cb(event_task("jira:issue_created"))
        check("Repeated created event does not re-create", fake.calls == [], fake.calls)
        cb(event_task("jira:issue_updated", priority="P1"))
        check("Update event syncs via shared engine", [c[0] for c in fake.calls] == ["topic", "send"], fake.calls)
        fake.calls.clear()
        cb(event_task("jira:issue_updated", key="CASE-4100"))
        check("Updated event for old untracked ticket ignored", fake.calls == [], fake.calls)

        payload = b'{"webhookEvent": "jira:issue_updated", "issue": {"key": "X-1"}}'
        WebhookHandler(WebhookConfig(secret="right")).handle_webhook(payload, "sha256=wrong")
        entry = read_log(tmp)[-1]
        check("Rejected signature is logged", entry["summary"]["final_status"] == "rejected")

        h = WebhookHandler(WebhookConfig(secret="right"))
        body = json.dumps({"webhookEvent": "jira:issue_updated", "issue": jira_issue()}).encode()
        check("Shared-secret header accepted", h.handle_webhook(body, "", token="right")["status"] == "queued")
        check("Wrong shared-secret header rejected", h.handle_webhook(body, "", token="nope")["status"] == "error")

        client = ws.create_webhook_app(WebhookConfig(secret="right")).test_client()
        check("Status visible locally", client.get("/webhooks/status").status_code == 200)
        check("Status hidden through a tunnel",
              client.get("/webhooks/status", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 404)
        check("Queue hidden through a tunnel",
              client.get("/webhooks/queue", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 404)
        check("Health still public",
              client.get("/webhooks/health", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 200)
except Exception as e:
    failed += 1
    print(f"✗ Sync callback logging tests failed: {type(e).__name__}: {e}")
finally:
    os.environ.pop("CTC_LOG_DIR", None)
    os.environ.pop("CTC_STATE_PATH", None)
    ws.SlackMessenger = real_messenger

# Summary

# Removed from a channel (e.g. after a token revoke): rejoin and retry once.
class ScriptedMessenger(ws.SlackMessenger):
    def __init__(self, replies):
        self.token, self.base_url, self.replies, self.sent = "xoxb-test", "", list(replies), []

    def _post(self, method, data):
        self.sent.append(method)
        return self.replies.pop(0)

m = ScriptedMessenger([{"ok": False, "error": "not_in_channel"}, {"ok": True}, {"ok": True, "ts": "1.2"}])
check("not_in_channel → rejoin → retry succeeds", m.post_message("C1", "hi") == "1.2", m.sent)
check("Rejoin uses conversations.join between attempts",
      m.sent == ["chat.postMessage", "conversations.join", "chat.postMessage"], m.sent)
m = ScriptedMessenger([{"ok": False, "error": "not_in_channel"}, {"ok": False, "error": "missing_scope"}])
check("Failed rejoin reports failure without retry loop", m.set_topic("C1", "t") is False and len(m.sent) == 2, m.sent)
m = ScriptedMessenger([{"ok": False, "error": "channel_not_found"}])
check("Other errors are not retried", m.archive_channel("C1") is False and m.sent == ["conversations.archive"], m.sent)


# Timeouts and capped retries (G11).
class FakeResp:
    def __init__(self, status, retry_after=None):
        self.status_code, self.headers = status, ({"Retry-After": retry_after} if retry_after else {})

def run_retry(responses, idempotent=False):
    calls, waits, original = [], [], ws.requests.request
    def fake(method, url, **kw):
        calls.append(kw.get("timeout"))
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    ws.requests.request = fake
    try:
        result = ws.request_with_retry("POST", "https://x/api", idempotent=idempotent, sleep=waits.append)
    except Exception as e:
        result = e
    finally:
        ws.requests.request = original
    return result, calls, waits

r, calls, waits = run_retry([FakeResp(429, "2"), FakeResp(200)])
check("429 retried after Retry-After", r.status_code == 200 and waits == [2.0], waits)
check("Every attempt has a timeout", calls and all(t == 15 for t in calls), calls)
r, calls, waits = run_retry([FakeResp(429, "900")] * 3)
check("Retries capped at 3 attempts", len(calls) == 3 and r.status_code == 429, len(calls))
check("Retry wait capped at 30s", max(waits) == 30, waits)
r, calls, _ = run_retry([FakeResp(503), FakeResp(200)])
check("5xx not retried for non-idempotent calls (no double post)", r.status_code == 503 and len(calls) == 1)
r, calls, _ = run_retry([FakeResp(503), FakeResp(200)], idempotent=True)
check("5xx retried for idempotent calls", r.status_code == 200 and len(calls) == 2)
r, calls, _ = run_retry([ws.requests.Timeout("t")])
check("Timeout on a post is raised, not retried", isinstance(r, ws.requests.Timeout) and len(calls) == 1)
r, calls, _ = run_retry([ws.requests.ConnectionError("c"), FakeResp(200)], idempotent=True)
check("Connection error retried for idempotent calls", getattr(r, "status_code", None) == 200)


# G5: the queue is drained by a worker; Jira is acknowledged before the Slack work runs.
import threading as _threading
def signed(secret, key):
    body = json.dumps({"webhookEvent": "jira:issue_updated", "issue": {"key": key, "fields": {
        "summary": "x", "status": {"name": "Open"}, "priority": {"name": "P3"},
        "project": {"key": key.split("-")[0]}, "customfield_10002": [{"name": "ISOC"}]}}}).encode()
    return body, "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

seen, gate = [], _threading.Event()
def slow_callback(task):
    gate.wait(5)
    seen.append(task["ticket_id"])
h = WebhookHandler(WebhookConfig(secret="s"), sync_callback=slow_callback)
body, sig = signed("s", "CASE-5001")
r = h.handle_webhook(body, sig)
check("Webhook answers before sync work finishes", r["status"] == "queued" and seen == [], seen)
gate.set()
for _ in range(50):
    if seen:
        break
    time.sleep(0.05)
check("Background worker drains the queue", seen == ["CASE-5001"] and h.queue.size() == 0, seen)
check("Processed counter increments", h.queue.stats()["processed"] == 1, h.queue.stats())

flaky_calls = []
def flaky(task):
    flaky_calls.append(task["attempts"])
    if task["attempts"] == 1:
        raise RuntimeError("Slack hiccup")
h2 = WebhookHandler(WebhookConfig(secret="s"), sync_callback=flaky)
h2.queue.enqueue({"ticket_id": "CASE-5002"})
h2.drain()
check("Failed task retried once, then succeeds", flaky_calls == [1, 2] and h2.queue.stats()["processed"] == 1,
      (flaky_calls, h2.queue.stats()))
def always_fail(task):
    raise RuntimeError("down")
h3 = WebhookHandler(WebhookConfig(secret="s"), sync_callback=always_fail)
h3.queue.enqueue({"ticket_id": "CASE-5003"})
check("Persistently failing task stops after 2 attempts", h3.drain() == 2 and h3.queue.stats()["failed"] == 1,
      h3.queue.stats())

app = ws.create_webhook_app(WebhookConfig(secret="s", max_queue_size=1))
client = app.test_client()
b1, s1 = signed("s", "CASE-5004")
b2, s2 = signed("s", "CASE-5005")
check("Accepted webhook returns 202", client.post("/webhooks/jira", data=b1, headers={"X-Hub-Signature": s1}).status_code == 202)
check("Full queue returns 503 so Jira retries",
      client.post("/webhooks/jira", data=b2, headers={"X-Hub-Signature": s2}).status_code == 503)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
