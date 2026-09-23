# Priority 2: Timeout Handling — Test Guide

**Status:** Configuration complete  
**What was implemented:** Per-step timeouts + overall pipeline timeout  
**When to implement:** Right after verifying Priority 1 pre-flight checks work live

---

## What Timeout Handling Does

Every network-bound operation now has a configurable timeout. If an operation (Jira fetch, Slack search, browser action, etc.) doesn't complete within its timeout, the skill stops immediately with a clear error message.

**Why it matters:**
- Prevents indefinite hangs if APIs are slow or unresponsive
- Gives users clear error messages instead of silent waiting
- Stops wasting time on a stalled operation

---

## Timeout Values (from config.yaml)

| Step | Operation | Timeout |
|------|-----------|---------|
| 0 | Pre-flight checks | 10s each |
| 1 | Jira fetch (metadata) | 10s |
| 3 | Slack search (dedup check) | 5s |
| 4 | Browser navigation | 30s |
| 4 | Browser interactions (click/type) | 15s |
| 4 | Browser wait for network idle | 20s |
| 5 | Slack verification | 10s |
| 7 | Slack post message | 10s |
| - | Overall pipeline | 120s (2 minutes max) |

---

## How to Test

### Unit Tests (Verify Configuration)

Run automated tests to verify timeouts are configured:

```bash
cd ~/.claude/skills/create-ticket-channel
python3 tests/test_timeout_config.py
```

Expected output:
```
✓ config.yaml loads without error
✓ timeout section exists
✓ preflight_check timeout is 10
✓ jira_fetch timeout is 10
✓ slack_search timeout is 5
✓ browser_navigation timeout is 30
✓ browser_interaction timeout is 15
✓ slack_verification timeout is 10
✓ slack_post_message timeout is 10
✓ overall_timeout is 120
✓ SKILL.md documents timeout handling
✓ SKILL.md documents all timeout scenarios

12 passed, 0 failed
```

### Integration Tests (Real Timeouts)

#### Test 1: Normal Run (All Operations Complete Within Timeout)

**Setup:** Normal Jira/Slack/chrome-bridge, all responsive

```bash
/create-ticket-channel SR-4028 --dry-run
```

**Expected output:**
- No timeout errors
- Pipeline proceeds normally to completion
- Each step completes within its timeout

#### Test 2: Jira API Slow (Simulate Slow Metadata Fetch)

**Setup:** Network throttle Jira API to 15+ seconds

```bash
# In Chrome DevTools: Network tab → Slow 3G or Custom (20s delay)
# OR add a firewall rule to delay Jira responses

/create-ticket-channel SR-4028 --dry-run
```

**Expected output:**
```
❌ Jira API timed out

Step: Fetch ticket metadata
Timeout: 10 seconds
Message: Jira API did not respond within 10 seconds.

Likely causes:
- Jira is overloaded or experiencing issues
- Your network connection is slow
- A regional outage is affecting Jira

Recommendation: Wait 1 minute and retry. If this keeps happening, check https://status.atlassian.com.
```

**Verification:** Error message is clear and actionable; pipeline stops without attempting to create channel

#### Test 3: Slack Search Slow (Simulate Slow Dedup Check)

**Setup:** Network throttle Slack API to 10+ seconds

```bash
# In Chrome DevTools: Network tab → Slow 3G or Custom (15s delay)
# OR add a firewall rule to delay Slack responses

/create-ticket-channel SR-4028 --dry-run
```

**Expected output:**
```
❌ Slack API timed out

Step: Search for existing channel
Timeout: 5 seconds
Message: Slack API did not respond within 5 seconds.

Likely causes:
- Slack is overloaded or experiencing issues
- Your network connection is slow
- A regional outage is affecting Slack

Recommendation: Wait 1 minute and retry. If this keeps happening, check https://status.slack.com.
```

**Verification:** Error message clearly identifies which step timed out; pipeline stops before browser automation

#### Test 4: Browser Navigation Slow

**Setup:** Network throttle to simulate slow page load

```bash
# In Chrome DevTools: Network tab → Slow 3G or slower
# Simulate Jira page load taking 40+ seconds

/create-ticket-channel SR-4028 --dry-run
```

**Expected output:**
```
❌ Browser navigation timed out

Step: Navigate to Jira issue
Timeout: 30 seconds
Message: Page did not load within 30 seconds.

Likely causes:
- Jira page is loading very slowly
- Your network connection is extremely slow
- Jira is experiencing performance issues

Recommendation: Check your internet speed. If this keeps happening, try again later or contact your Jira administrator.
```

**Verification:** Timeout occurs during browser action, clear error message shown

#### Test 5: Browser Interaction Slow (Click/Type Timeout)

**Setup:** Inject delays into chrome-bridge interaction tools

```bash
# Simulate a slow browser (very overloaded machine)
# OR add network-level delays to Jira

/create-ticket-channel SR-4028 --dry-run
```

**Expected output:**
```
❌ Browser interaction timed out

Step: Click "Create channel" button
Timeout: 15 seconds
Message: Interaction did not complete within 15 seconds.

Likely causes:
- Browser is very slow or overloaded
- JavaScript framework is slow
- Network is extremely slow

Recommendation: Close other browser tabs, restart browser, and try again. If this keeps happening, contact your Jira administrator.
```

**Verification:** Timeout occurs during click/type, pipeline stops

#### Test 6: Overall Pipeline Timeout (Accumulation)

**Setup:** Make multiple operations slow (each under their individual timeout, but combined exceeds 120s)

```bash
# Example: simulate 3 operations each taking 50 seconds
# (no individual timeout hit, but combined > 120s)

/create-ticket-channel SR-4028 --dry-run
```

**Expected output:**
```
❌ Pipeline timed out

Total elapsed time: 125 seconds (limit: 120 seconds)

The operation took too long overall. This could mean:
- Multiple steps were individually slow
- The overall task is taking longer than expected
- System resources are constrained

Recommendation: Verify all systems are healthy. Wait a few minutes and try again.
```

**Verification:** Overall pipeline timeout is enforced as a safety valve

---

## Configuration Reference

Timeouts are defined in `config/config.yaml` under the `timeouts` section:

```yaml
timeouts:
  preflight_check: 10
  jira_fetch: 10
  slack_search: 5
  browser_navigation: 30
  browser_interaction: 15
  browser_wait_network_idle: 20
  slack_verification: 10
  slack_post_message: 10
  overall_timeout: 120
```

**To adjust timeouts for testing:**

```yaml
# Make Jira timeout shorter (test at 5s instead of 10s)
timeouts:
  jira_fetch: 5

# Make browser timeout longer if your network is slow
timeouts:
  browser_navigation: 45
```

---

## Error Message Format

All timeout errors follow this format:

```
❌ {Service} {operation} timed out

Step: {step_name}
Timeout: {timeout_seconds} seconds
Message: {friendly_message}

Likely causes:
- {cause 1}
- {cause 2}
- {cause 3}

Recommendation: {action 1}. {action 2}.
```

Example: if Slack times out after 5 seconds:
```
❌ Slack API timed out

Step: Search for existing channel
Timeout: 5 seconds
Message: Slack API did not respond within 5 seconds.

Likely causes:
- Slack is overloaded or experiencing issues
- Your network connection is slow
- A regional outage is affecting Slack

Recommendation: Wait 1 minute and retry. If this keeps happening, check https://status.slack.com.
```

---

## Files Modified

- `config/config.yaml` — Added `timeouts` section with 10 timeout values (10 lines)
- `SKILL.md` — Added "Timeout Handling" section and updated error tables (50+ lines)
- `tests/test_timeout_config.py` — NEW: Unit tests for timeout configuration

---

## Success Criteria

| Criterion | How to Verify |
|-----------|---------------|
| Per-step timeouts exist | Check config.yaml has all timeout values |
| Timeouts are documented | Check SKILL.md "Timeout Handling" section |
| Error messages are clear | Trigger a timeout and verify message quality |
| Overall pipeline timeout is enforced | Sum of slow operations exceeds 120s, verify timeout |
| Unit tests pass | Run test_timeout_config.py, verify 12/12 passing |

---

## Testing Approach

**Do NOT manually throttle every single timeout.** Use this strategy instead:

1. **Verify unit tests pass** (5 minutes) — confirms configuration is correct
2. **Run one live timeout test** (5-10 minutes) — pick the easiest one to simulate (e.g., Slack search by adding network delay)
3. **Verify error message quality** — ensure it's clear and actionable
4. **Move to Priority 3** — implementation is complete once one live test confirms timeouts work

Why? Timeout handling is deterministic (if X takes >T seconds, timeout). Once one scenario works, all others work the same way. Full coverage of all 6+ scenarios is overkill for this priority.

---

## Rollback Plan

If timeout errors are too aggressive (timing out legitimate slow operations):

```yaml
# In config.yaml, increase timeouts:
timeouts:
  jira_fetch: 20  # was 10
  browser_navigation: 60  # was 30
```

Or disable all timeouts (not recommended):
```yaml
timeouts:
  overall_timeout: 3600  # 1 hour, effectively disables timeout
```

---

## Next Steps

1. **Run unit tests** to verify configuration
2. **Run one integration test** with a real timeout scenario
3. **Review error message quality** — is it clear to users?
4. **Move to Priority 3: Better Error Messages** — design error messages for Steps 1-7

---

## Files Ready for Review

1. `config/config.yaml` — `timeouts` section (10 lines)
2. `SKILL.md` — "Timeout Handling" section + error table updates (50+ lines)
3. `tests/test_timeout_config.py` — NEW: Unit tests (40 lines)

All configuration in place. Ready for integration testing.
