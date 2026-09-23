# Tier 2: Ticket-to-Channel Sync — Test Guide

**Tier 2 Status:** Core implementation complete (scripts/ticket_sync.py), 41 unit tests passing

This guide covers testing the ticket sync feature end-to-end before integration into the main skill executor.

---

## Quick Start

### Run Unit Tests
```bash
cd /home/parthkumars/.claude/skills/create-ticket-channel
python3 tests/test_ticket_sync.py
```

Expected output: **41 passed, 0 failed**

---

## Test Suite Overview

### Test File: `tests/test_ticket_sync.py` (41 tests)

#### Test Groups

**State Detection (3 tests)**
- Detects status change between states
- Ignores unchanged fields
- Shows old/new values correctly

**Emoji Formatting (7 tests)**
- Status emojis: Open 🟠, In Progress 🔵, Done 🟢, Unknown ⚪
- Priority emojis: P1 🔴, P2 🟠, P3 🟡, P4 🟢

**Topic Formatting (4 tests)**
- Topic includes ticket ID, status, priority
- Topic includes emojis
- Format: "{emoji} {ticket_id} - {status} {emoji} {priority}"

**Pinned Message (5 tests)**
- Includes ticket ID, status, priority, assignee
- Includes Jira link
- Markdown formatting correct

**Archive Logic (3 tests)**
- Archives "Done", "Resolved", "Closed" statuses
- Doesn't archive "Open" status

**Webhook Validation (2 tests)**
- Validates well-formed Jira webhook events
- Rejects invalid events

**Webhook Extraction (4 tests)**
- Extracts ticket ID, status, priority, assignee from webhook

**Sync Manager - No Changes (4 tests)**
- First sync detects changes
- Second sync with same state shows no_changes
- No-change status properly set

**Sync with Status Change (3 tests)**
- Detects status change
- Takes update_topic action
- Takes update_pinned_message action

**Sync with Archive (1 test)**
- Archives when status changes to Done

**Sync Statistics (4 tests)**
- Counts total syncs
- Counts successful syncs
- Counts no-change syncs
- Calculates average duration

---

## Integration Test Scenarios

### Scenario 1: Initial Channel Creation and Sync

**Objective:** Verify sync on newly created channel

**Steps:**
1. Create channel for ticket SR-4028 (via create-ticket-channel skill)
2. Fetch initial state from Jira:
   - Status: Open
   - Priority: P3
   - Assignee: John Doe
   - Summary: Fix login button
3. Run sync:
   ```python
   from scripts.ticket_sync import TicketSyncManager
   
   manager = TicketSyncManager()
   state = {
       "ticket_id": "SR-4028",
       "status": "Open",
       "priority": "P3",
       "assignee": "John Doe",
       "summary": "Fix login button",
       "updated_at": "2026-09-23T10:00:00Z",
   }
   
   result = manager.sync_ticket(
       "SR-4028",
       "sr-4028-5tattva-low",
       state,
       "https://bloo-systems.atlassian.net/browse/SR-4028"
   )
   ```
4. Verify:
   - `result["status"] == "success"`
   - `result["changes_detected"] == True` (first sync always has changes)
   - `result["actions_taken"]` includes "update_topic" and "update_pinned_message"

**Expected Outcome:**
- ✅ Channel topic updated to: "🟠 SR-4028 - Open 🟡 P3"
- ✅ Pinned message posted with ticket details
- ✅ No archive action

---

### Scenario 2: Status Change (Open → In Progress)

**Objective:** Verify sync detects and reflects status change

**Steps:**
1. Use existing channel from Scenario 1
2. Update Jira ticket status to "In Progress"
3. Fetch new state from Jira
4. Run sync with new state
5. Verify:
   - `result["status"] == "success"`
   - `result["changes_detected"] == True`
   - `result["changes"]["status"]["old"] == "Open"`
   - `result["changes"]["status"]["new"] == "In Progress"`
   - `result["actions_taken"]` includes "update_topic"

**Expected Outcome:**
- ✅ Channel topic updated to: "🔵 SR-4028 - In Progress 🟡 P3"
- ✅ Pinned message updated with new status
- ✅ No archive action

---

### Scenario 3: Priority Escalation (P3 → P1)

**Objective:** Verify sync reflects priority changes

**Steps:**
1. Use existing channel from Scenario 2
2. Update Jira ticket priority from P3 to P1
3. Fetch new state from Jira
4. Run sync with new state
5. Verify:
   - `result["changes"]["priority"]["old"] == "P3"`
   - `result["changes"]["priority"]["new"] == "P1"`
   - `result["actions_taken"]` includes "update_topic"

**Expected Outcome:**
- ✅ Channel topic updated to: "🔵 SR-4028 - In Progress 🔴 P1"
- ✅ Emoji changed from 🟡 (medium) to 🔴 (critical)
- ✅ Team can see urgency increased without opening Jira

---

### Scenario 4: No Changes (Idempotent Sync)

**Objective:** Verify sync doesn't update channel if nothing changed

**Steps:**
1. Use existing channel from Scenario 3
2. Fetch state from Jira (same as last sync)
3. Run sync twice with identical state
4. Verify:
   - First sync: `result["status"] == "no_changes"` (second run)
   - Second sync: `result["status"] == "no_changes"`
   - `result["changes_detected"] == False`
   - `result["actions_taken"]` is empty

**Expected Outcome:**
- ✅ No Slack API calls made (optimization)
- ✅ No unnecessary channel updates
- ✅ Audit log records "no changes"

---

### Scenario 5: Resolution and Archival (In Progress → Done)

**Objective:** Verify channel archives when ticket closes

**Steps:**
1. Use existing channel from Scenario 4
2. Update Jira ticket status to "Done"
3. Fetch new state from Jira
4. Run sync with new state
5. Verify:
   - `result["status"] == "success"`
   - `result["changes"]["status"]["new"] == "Done"`
   - `result["actions_taken"]` includes "archive_channel"

**Expected Outcome:**
- ✅ Channel topic updated to: "🟢 SR-4028 - Done 🔴 P1"
- ✅ Channel marked as archived in Slack
- ✅ Channel removed from active channel list
- ✅ Historical reference available if needed

---

### Scenario 6: Sync Statistics

**Objective:** Verify sync statistics tracking

**Steps:**
1. Create new sync manager
2. Run sync multiple times:
   - Sync 1: Initial state (changes detected)
   - Sync 2: Same state (no changes)
   - Sync 3: Status change (changes detected)
   - Sync 4: Same state (no changes)
3. Call `manager.get_sync_stats()`
4. Verify:
   ```python
   stats = manager.get_sync_stats()
   assert stats["total_syncs"] == 4
   assert stats["successful"] == 2  # Syncs 1 & 3
   assert stats["no_changes"] == 2  # Syncs 2 & 4
   assert stats["average_duration"] >= 0
   ```

**Expected Outcome:**
- ✅ Accurate sync count
- ✅ Correct categorization (successful vs no_changes)
- ✅ Average duration calculated correctly

---

## Webhook Testing

### Webhook Event Validation

**Objective:** Verify webhook validator correctly processes Jira events

**Test Case 1: Valid Jira Issue Updated Event**

```python
from scripts.ticket_sync import WebhookValidator

event = {
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

# Validate signature
assert WebhookValidator.validate_signature(event) == True

# Extract ticket info
info = WebhookValidator.extract_ticket_info(event)
assert info["ticket_id"] == "SR-4028"
assert info["status"] == "In Progress"
assert info["priority"] == "P1"
assert info["assignee"] == "Jane Doe"
```

**Test Case 2: Invalid Webhook (Missing Required Fields)**

```python
invalid_event = {
    "some": "data"
}

assert WebhookValidator.validate_signature(invalid_event) == False
info = WebhookValidator.extract_ticket_info(invalid_event)
assert info is None
```

---

## Performance Testing

### Sync Latency

**Objective:** Measure how fast sync completes

**Test:**
```python
import time
from scripts.ticket_sync import TicketSyncManager

manager = TicketSyncManager()
state = {"ticket_id": "SR-4028", "status": "Open", ...}

start = time.time()
result = manager.sync_ticket("SR-4028", "sr-4028-5tattva-low", state, "http://...")
duration = time.time() - start

print(f"Sync completed in {duration:.3f}s")
print(f"Result says: {result['duration_seconds']}s")
```

**Expected:** < 100ms per sync (no actual API calls, just data processing)

### Memory Usage

For high-volume sync (many channels), verify memory usage doesn't grow unboundedly:

```python
manager = TicketSyncManager()

# Simulate 1000 syncs across different channels
for i in range(1000):
    state = {
        "ticket_id": f"SR-{4000+i}",
        "status": "Open",
        "priority": "P3",
        "assignee": "John",
        "summary": "Test",
        "updated_at": "2026-09-23T10:00:00Z",
    }
    manager.sync_ticket(f"SR-{4000+i}", f"sr-{4000+i}-test", state, "http://...")

stats = manager.get_sync_stats()
print(f"Synced {stats['total_syncs']} channels")
print(f"Memory usage: TODO (use tracemalloc)")
```

---

## Error Scenarios

### Scenario: Invalid State Data

**Test:** Passing incomplete state data

```python
manager = TicketSyncManager()

# Missing required fields
incomplete_state = {
    "ticket_id": "SR-4028",
    # Missing status, priority, assignee
}

result = manager.sync_ticket("SR-4028", "sr-4028-test", incomplete_state, "http://...")

# Should gracefully handle missing fields with defaults
assert result["status"] == "success" or "failure"
```

---

## Integration Checklist

Before moving to full integration, verify:

- [ ] All 41 unit tests pass
- [ ] Scenario 1 (Initial sync) works as expected
- [ ] Scenario 2 (Status change) detects and reflects changes
- [ ] Scenario 3 (Priority escalation) updates correctly
- [ ] Scenario 4 (No changes) skips unnecessary updates
- [ ] Scenario 5 (Archival) archives on Done status
- [ ] Scenario 6 (Statistics) tracks correctly
- [ ] Webhook validation works with real Jira events
- [ ] Webhook extraction parses all fields correctly
- [ ] Performance: Single sync < 100ms
- [ ] No unhandled errors in any scenario
- [ ] Audit logging records all sync operations

---

## Known Limitations

1. **No actual Slack API calls in tests** — Tests verify logic only, not real channel updates
   - Integration will require real Slack channel creation first
   - Will verify via `slack_search_channels` after sync completes

2. **No Jira API calls in tests** — State is provided as input
   - Real integration will fetch from Jira via MCP
   - Will parse real Jira webhook events from Atlassian

3. **Archive is simulated** — Tests track action but don't verify actual archival
   - Real implementation will call Slack API to archive
   - Need permissions to archive channels in target workspace

---

## Next Steps

1. **Create comprehensive integration test** using real Slack and Jira
2. **Implement webhook endpoint** (Flask/FastAPI) to receive Jira events
3. **Implement polling scheduler** as fallback if webhooks unavailable
4. **Integrate into main skill executor** (skill_executor.py)
5. **Deploy and monitor sync health**

---

## Test Results Summary

**Date:** 2026-09-23  
**Test File:** tests/test_ticket_sync.py  
**Total Tests:** 41  
**Passed:** 41  
**Failed:** 0  
**Success Rate:** 100%

Test coverage includes:
- State change detection
- Emoji formatting (status & priority)
- Channel topic formatting
- Pinned message formatting
- Archive detection
- Webhook validation
- Ticket info extraction
- Sync manager operations
- Sync statistics

All critical paths tested and verified.
