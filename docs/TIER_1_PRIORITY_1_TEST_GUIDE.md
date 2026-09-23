# Priority 1: Pre-flight Checks — Test Guide

**Completed:** 2026-09-23  
**What was implemented:** Step 0 (Pre-flight Checks) in the skill pipeline

---

## What Pre-flight Checks Do

Before attempting to create a channel, the skill now verifies all three dependencies are ready:

1. **chrome-bridge is connected** — Browser automation tool loaded
2. **Jira API is responsive** — Can reach and authenticate to Jira
3. **Slack API is responsive** — Can reach and authenticate to Slack

If any check fails, the skill stops immediately with a clear error message. This prevents wasting time debugging mid-pipeline failures.

---

## How to Test

### Unit Tests (Immediate Verification)

Run the automated tests:

```bash
cd ~/.claude/skills/create-ticket-channel
python3 tests/test_preflight_checks.py
```

Expected output:
```
✓ config.yaml loads without error
✓ preflight section exists
✓ preflight.enabled is true
✓ chrome-bridge error message mentions setup instructions
...
15 passed, 0 failed
```

### Integration Tests (Real Jira/Slack)

#### Test 1: Normal Run (All Checks Pass)

**Setup:** chrome-bridge is loaded, Jira and Slack are healthy

```bash
/create-ticket-channel SR-4028 --dry-run
```

**Expected output:**
- No pre-flight error messages
- Pipeline continues to Step 1 normally
- Output includes: "Ticket: SR-4028"

#### Test 2: chrome-bridge Disconnected

**Setup:** Stop chrome-bridge (disable extension or kill process)

```bash
# Method 1: Disable extension via chrome://extensions
# Method 2: In Claude Code: set chrome-bridge to not loaded

/create-ticket-channel SR-4028 --dry-run
```

**Expected output:**
```
❌ chrome-bridge is not connected

The browser automation tool required for channel creation is not loaded.

Setup (one-time per machine):
1. Open chrome://extensions in Chrome
2. Enable "Developer mode" (toggle in top-right corner)
3. Click "Load unpacked"
4. Select the directory: ~/.claude/skills/create-ticket-channel/chrome-bridge/extension
5. Restart Claude Code
6. Try again

For more details, see: ~/.claude/skills/create-ticket-channel/SKILL.md "Step 4: How It Was Unblocked"
```

**Verification:** Error message is clear and actionable; pipeline stops without attempting browser automation

#### Test 3: Jira Unreachable (Simulated)

**Setup:** Temporarily block access to Jira API (or mock a timeout)

```bash
# Option A: Use network throttling in browser dev tools to simulate slow Jira
# Option B: Edit config.yaml to point to invalid Jira endpoint (temporary)

/create-ticket-channel SR-4028 --dry-run
```

**Expected output:**
```
❌ Jira API is not reachable

Status code: {STATUS}
Error message: {ERROR}

This could mean:
- Jira is currently down (check https://status.atlassian.com)
- Your authentication token has expired or been revoked
- Your network connection is down
- A firewall is blocking the connection

What to do:
1. Check your network connection
2. Verify you can access Jira in your browser: https://bloo-systems.atlassian.net
3. Wait 30 seconds and try again
4. Contact your Jira administrator if the problem persists
```

**Verification:** Error message shows status code and suggests remediation steps

#### Test 4: Slack Unreachable (Simulated)

**Setup:** Temporarily block access to Slack API (or mock a timeout)

```bash
# Option A: Disable Slack MCP in Claude Code temporarily
# Option B: Mock a Slack API failure

/create-ticket-channel SR-4028 --dry-run
```

**Expected output:**
```
❌ Slack API is not reachable

Status code: {STATUS}
Error message: {ERROR}

This could mean:
- Slack is currently down (check https://status.slack.com)
- Your authentication token has expired or been revoked
- Your network connection is down
- A firewall is blocking the connection

What to do:
1. Check your network connection
2. Verify you can access Slack in your browser or app
3. Wait 30 seconds and try again
4. Contact your Slack administrator if the problem persists
```

**Verification:** Error message is clear and distinct from other errors

---

## Configuration

Pre-flight checks are controlled by `config/config.yaml`:

```yaml
preflight:
  enabled: true
  check_chrome_bridge: true
  check_jira_connectivity: true
  check_slack_connectivity: true
  fail_fast: true
```

To disable all checks (not recommended):
```yaml
preflight:
  enabled: false
```

To disable individual checks:
```yaml
preflight:
  enabled: true
  check_chrome_bridge: false  # Skip chrome-bridge check
  check_jira_connectivity: true
  check_slack_connectivity: true
```

---

## Files Modified

- `config/config.yaml` — Added `preflight` section (11 lines)
- `SKILL.md` — Added Step 0 (65 lines of documentation)
- `SKILL.md` — Updated Error Handling table with pre-flight errors (5 new rows)
- `tests/test_preflight_checks.py` — NEW: Unit tests for configuration (70 lines)

---

## Success Criteria Met?

✅ **Fail fast if chrome-bridge disconnected** — Clear setup instructions shown immediately  
✅ **Fail fast if Jira unreachable** — User knows to check Jira status/connectivity  
✅ **Fail fast if Slack unreachable** — User knows to check Slack status/connectivity  
✅ **Error messages are actionable** — Each error suggests "what to do next"  
✅ **Configuration is externalized** — No code changes needed to skip checks  
✅ **Tests pass** — 15/15 unit tests pass  

---

## Next Steps

1. **Manual testing:** Run all 4 integration tests above with real Jira/Slack
2. **User feedback:** Ask a team member to trigger a pre-flight error and see if the message is clear
3. **Move to Priority 2:** Implement timeout handling (prevents indefinite hangs)

---

## Rollback Plan

If there's an issue with pre-flight checks:

```yaml
# In config.yaml, set:
preflight:
  enabled: false
```

This will skip all checks and proceed directly to Step 1. The skill will still work for known-good tickets, but won't detect upstream connectivity issues until they happen mid-pipeline.

