#!/usr/bin/env python3
"""
Error message generator for create-ticket-channel skill.

Provides formatted, actionable error messages for all failure scenarios
in Steps 1-7. Each error includes:
- What went wrong
- Why it happened (likely causes)
- What to do about it (remediation steps)

Usage:
    from error_messages import error_message

    msg = error_message("ticket_not_found", ticket_id="SR-4028")
    print(msg)
"""


def error_message(error_type, **kwargs):
    """
    Generate a formatted error message.

    Args:
        error_type: Type of error (see ERRORS dict below)
        **kwargs: Error-specific parameters (ticket_id, status_code, etc.)

    Returns:
        Formatted error message string
    """
    if error_type not in ERRORS:
        return f"Unknown error type: {error_type}"

    error_template = ERRORS[error_type]
    return error_template(**kwargs)


# ============================================================================
# STEP 1 ERRORS: Fetch Jira Metadata
# ============================================================================

def _ticket_not_found(ticket_id):
    """Ticket doesn't exist in Jira."""
    return f"""❌ Ticket not found

Step: Step 1 — Fetch Jira metadata
Ticket: {ticket_id}

Why this happened:
- The ticket ID is incorrect or misspelled
- The ticket was deleted from Jira
- You don't have permission to view this ticket

What to do:
1. Verify the ticket ID is correct (e.g., SR-4028, not SR4028)
2. Search for the ticket in Jira: https://bloo-systems.atlassian.net
3. Ensure you have permission to view this ticket in Jira
4. Try again with the correct ticket ID
"""


def _customer_missing(ticket_id):
    """Required customer field is empty or missing."""
    return f"""❌ Customer information missing

Step: Step 1 — Fetch Jira metadata
Ticket: {ticket_id}
Field: Organization/Customer

Why this happened:
- The customer/organization field is empty in Jira
- The field value is in an unexpected format
- Jira configuration may have changed

What to do:
1. Open the ticket in Jira: https://bloo-systems.atlassian.net/browse/{ticket_id}
2. Locate the "Organization(s)" field
3. Ensure it has a value (e.g., "5Tattva", "CanFin")
4. Save the ticket
5. Try creating the channel again
"""


def _priority_invalid(ticket_id, priority_value):
    """Priority field has unrecognized value."""
    return f"""❌ Priority not recognized

Step: Step 1 — Fetch Jira metadata
Ticket: {ticket_id}
Priority: {priority_value}

Why this happened:
- The priority value is not in the standard P1-P4 format
- The Jira configuration may have changed
- Priority mapping is incomplete

What to do:
1. Open the ticket in Jira: https://bloo-systems.atlassian.net/browse/{ticket_id}
2. Check the "Priority" field
3. Ensure it's one of: P1, P2, P3, P4
4. If the priority is correct, this may indicate a Jira configuration change
5. Report this issue with the priority value above
"""


# ============================================================================
# STEP 3 ERRORS: Check for Existing Channel
# ============================================================================

def _channel_already_exists(ticket_id, channel_name):
    """Channel already exists in Slack."""
    return f"""ℹ️ Channel already exists

Step: Step 3 — Check for existing channel
Ticket: {ticket_id}
Channel: #{channel_name}

This channel already exists in Slack, so no new channel was created.

What to do:
- Use the existing channel: #{channel_name}
- All discussion for this ticket goes in that channel
- No further action needed
"""


def _slack_search_error(ticket_id, error_code, error_text):
    """Slack API error during channel search."""
    return f"""❌ Slack API error

Step: Step 3 — Search for existing channel
Ticket: {ticket_id}
Status code: {error_code}
Error: {error_text}

Why this happened:
- Slack API returned an error
- Your Slack authentication may be expired
- A network issue prevented the search
- Slack may be experiencing issues

What to do:
1. Check Slack status: https://status.slack.com
2. Verify your Slack authentication is current
3. Check your network connection
4. Wait a few minutes and try again
5. If this persists, contact your Slack administrator
"""


# ============================================================================
# STEP 4 ERRORS: Browser Automation
# ============================================================================

def _trigger_button_not_found(ticket_id):
    """'Open Slack discussions' button not found."""
    return f"""❌ Browser automation failed

Step: Step 4 — Locate 'Open Slack discussions' button
Ticket: {ticket_id}
Element: 'Open Slack discussions' button

Why this happened:
- The button location may have changed
- The Jira UI may have been updated
- The page didn't fully load
- The 'Slack for Jira' app may not be installed

What to do:
1. Verify the 'Slack for Jira' app is installed in your Jira instance
2. Open the ticket in Jira: https://bloo-systems.atlassian.net/browse/{ticket_id}
3. Look for the 'Open Slack discussions' button in the issue view
4. If the button exists but we can't find it, the Jira UI may have changed
5. Report this issue with a screenshot of the button location
"""


def _iframe_not_found(ticket_id):
    """Slack integration iframe not found."""
    return f"""❌ Browser automation failed

Step: Step 4 — Locate Slack integration panel
Ticket: {ticket_id}
Element: Slack integration iframe

Why this happened:
- The Slack panel didn't open after clicking the button
- The iframe location changed in a Jira update
- JavaScript didn't execute properly
- The 'Slack for Jira' app may not be responding

What to do:
1. Open the ticket manually in Jira
2. Click the 'Open Slack discussions' button
3. Verify that the Slack panel opens on the right side
4. If the panel doesn't open, the app may need to be reinstalled
5. Try creating the channel again
"""


def _create_button_not_found(ticket_id):
    """Channel creation button not found in Slack panel."""
    return f"""❌ Browser automation failed

Step: Step 4 — Locate 'Create channel' button
Ticket: {ticket_id}
Element: 'Create another channel' or 'Create new channel' button

Why this happened:
- The button location changed in the Slack for Jira app update
- The Slack panel didn't fully load
- The app already shows an existing channel (in which case, no create button exists)

What to do:
1. Open the ticket in Jira: https://bloo-systems.atlassian.net/browse/{ticket_id}
2. Click 'Open Slack discussions'
3. Look for the 'Create channel' button in the panel
4. If an existing channel is already shown, no new channel is needed
5. Try creating the channel again
"""


def _browser_interaction_failed(ticket_id, action, details=""):
    """Click, type, or other browser interaction failed."""
    extra = f"\nDetails: {details}" if details else ""
    return f"""❌ Browser interaction failed

Step: Step 4 — Browser automation
Ticket: {ticket_id}
Action: {action}
Status: Failed after 3 retries{extra}

Why this happened:
- The page may not have fully loaded
- JavaScript may not have executed
- The element may have moved or changed
- The browser may be unresponsive

What to do:
1. Verify the Jira page loads correctly in your browser
2. Ensure JavaScript is enabled
3. Close other browser tabs to free up memory
4. Try creating the channel again
5. If this keeps happening, restart your browser
"""


def _browser_navigation_slow(ticket_id):
    """See timeout_wrapper for timeout-specific errors."""
    return f"""⏱️ Jira page load is slow

Step: Step 4 — Navigate to Jira issue
Ticket: {ticket_id}
Timeout: 30 seconds

The page is taking longer than expected to load.

Why this happened:
- Your internet connection is slow
- Jira is responding slowly
- Your browser may be overloaded

What to do:
1. Check your internet speed
2. Close other browser tabs
3. Wait a moment and try again
4. Check if Jira is experiencing performance issues
5. Try again later if Jira is slow
"""


# ============================================================================
# STEP 5 ERRORS: Verify Channel Creation
# ============================================================================

def _channel_verification_failed(ticket_id, channel_name, search_result):
    """Channel not found in Slack after creation."""
    return f"""❌ Channel creation could not be verified

Step: Step 5 — Verify channel in Slack
Ticket: {ticket_id}
Channel: {channel_name}
Search result: {search_result}

Why this happened:
- The channel may have been created but search isn't finding it yet (lag)
- The channel name may be different than expected
- The channel may have been created in a different workspace
- Slack indexing may be delayed

What to do:
1. Search for the channel in Slack: /browse #{channel_name}
2. If you find it, the channel was created successfully
3. If you can't find it, the creation may have failed
4. Check your Jira audit log to see if channel creation was attempted
5. Try creating the channel again
"""


def _channel_search_error(ticket_id, error_code, error_text):
    """Slack API error during channel verification."""
    return f"""❌ Slack API error

Step: Step 5 — Verify channel in Slack
Ticket: {ticket_id}
Status code: {error_code}
Error: {error_text}

Why this happened:
- Slack API returned an error
- Your authentication may be expired
- A network issue prevented the search
- Slack may be experiencing issues

What to do:
1. Check Slack status: https://status.slack.com
2. Verify your authentication is current
3. Wait a moment and try again
4. If this keeps happening, contact your Slack administrator
"""


# ============================================================================
# STEP 7 ERRORS: Post Starter Message
# ============================================================================

def _message_post_error(ticket_id, channel_name, error_code, error_text):
    """Slack message post failed."""
    return f"""❌ Could not post starter message

Step: Step 7 — Post starter message
Ticket: {ticket_id}
Channel: #{channel_name}
Status code: {error_code}
Error: {error_text}

Why this happened:
- Slack API returned an error
- Your authentication may be invalid
- The channel may not exist or be inaccessible
- A network issue prevented the post

What to do:
1. Verify you have permission to post in #{channel_name}
2. Check that the channel exists: /browse #{channel_name}
3. Check Slack status: https://status.slack.com
4. Try posting the message manually: /msg #{channel_name} "..."
5. Try again
"""


def _message_post_timeout(ticket_id, channel_name):
    """See timeout_wrapper for timeout-specific errors."""
    return f"""⏱️ Slack message post timed out

Step: Step 7 — Post starter message
Ticket: {ticket_id}
Channel: #{channel_name}
Timeout: 10 seconds

Why this happened:
- Slack API is responding slowly
- Your network connection is slow
- Slack may be experiencing issues

What to do:
1. Check Slack status: https://status.slack.com
2. Post the message manually: /msg #{channel_name} "..."
3. Try again after a few minutes
"""


# ============================================================================
# ERROR REGISTRY
# ============================================================================

ERRORS = {
    # Step 1 errors
    "ticket_not_found": _ticket_not_found,
    "customer_missing": _customer_missing,
    "priority_invalid": _priority_invalid,

    # Step 3 errors
    "channel_already_exists": _channel_already_exists,
    "slack_search_error": _slack_search_error,

    # Step 4 errors
    "trigger_button_not_found": _trigger_button_not_found,
    "iframe_not_found": _iframe_not_found,
    "create_button_not_found": _create_button_not_found,
    "browser_interaction_failed": _browser_interaction_failed,
    "browser_navigation_slow": _browser_navigation_slow,

    # Step 5 errors
    "channel_verification_failed": _channel_verification_failed,
    "channel_search_error": _channel_search_error,

    # Step 7 errors
    "message_post_error": _message_post_error,
    "message_post_timeout": _message_post_timeout,
}


if __name__ == "__main__":
    # Demo: print all error messages
    print("=" * 70)
    print("SAMPLE ERROR MESSAGES")
    print("=" * 70)

    print("\n[Step 1] Ticket not found:")
    print(error_message("ticket_not_found", ticket_id="SR-4028"))

    print("[Step 3] Channel already exists:")
    print(error_message("channel_already_exists", ticket_id="SR-4028", channel_name="sr-4028-5tattva-low"))

    print("[Step 4] Trigger button not found:")
    print(error_message("trigger_button_not_found", ticket_id="SR-4028"))

    print("[Step 5] Channel verification failed:")
    print(error_message("channel_verification_failed", ticket_id="SR-4028", channel_name="sr-4028-5tattva-low", search_result="Not found"))
