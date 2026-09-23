#!/usr/bin/env python3
"""
Enhanced audit logging for create-ticket-channel skill.

Records step-by-step execution details for every pipeline run, including:
- Pre-flight check results
- Jira metadata fetching
- Channel name generation
- Slack dedup check
- Browser automation actions
- Verification results
- Message posting
- Overall performance metrics

Each run gets a unique run_id and detailed step records with timing.

Usage:
    from audit_logger import AuditLogger

    logger = AuditLogger("SR-4028", "SR", "5Tattva", "P3")
    logger.record_step(0, "Pre-flight Checks", "success", 2.1,
                       details={"chrome_bridge": "connected", ...})
    logger.record_action(4, "click_trigger", 1.2, {"retries": 0})
    logger.finalize("sr-4028-5tattva-low", "success")
"""

import json
import time
import uuid
import os
from datetime import datetime
from pathlib import Path


def generate_run_id():
    """Generate a unique run ID."""
    now = datetime.now()
    timestamp = now.strftime("%Y%m%d-%H%M%S")
    suffix = str(uuid.uuid4())[:8]
    return f"{timestamp}-{suffix}"


class AuditLogger:
    """Enhanced audit logger for skill execution."""

    def __init__(self, ticket_id, ticket_type, customer, priority, log_path=None):
        """Initialize audit logger."""
        self.run_id = generate_run_id()
        self.ticket_id = ticket_id
        self.ticket_type = ticket_type
        self.customer = customer
        self.priority = priority
        self.steps = []
        self.start_time = time.time()
        self.current_step_actions = {}

        if log_path is None:
            log_path = Path.home() / ".claude" / "skills" / "create-ticket-channel" / "audit.log.jsonl"
        self.log_path = Path(log_path)

    def record_step(self, step_num, step_name, status, duration_seconds, details=None, error=None):
        """
        Record a step's execution.

        Args:
            step_num: Step number (0-7)
            step_name: Human-readable step name
            status: "success", "failure", "timeout", or "skipped"
            duration_seconds: How long the step took
            details: Step-specific data dict
            error: Error details dict (if failed)
        """
        step_record = {
            "step": step_num,
            "name": step_name,
            "status": status,
            "duration_seconds": round(duration_seconds, 2),
        }

        if details:
            step_record["details"] = details

        if error:
            step_record["error"] = error

        # Include actions if this step had sub-actions
        if step_num in self.current_step_actions:
            step_record["actions"] = self.current_step_actions[step_num]
            del self.current_step_actions[step_num]

        self.steps.append(step_record)

    def record_action(self, step_num, action_name, duration_seconds, details=None, status="success"):
        """
        Record a sub-action within a step (e.g., browser automation actions).

        Args:
            step_num: Which step this action belongs to
            action_name: Name of the action (e.g., "click_trigger", "type_channel_name")
            duration_seconds: How long the action took
            details: Action-specific data
            status: "success", "failure", "timeout"
        """
        if step_num not in self.current_step_actions:
            self.current_step_actions[step_num] = []

        action_record = {
            "action": action_name,
            "duration_seconds": round(duration_seconds, 2),
            "status": status,
        }

        if details:
            action_record.update(details)

        self.current_step_actions[step_num].append(action_record)

    def finalize(self, channel_name, final_status):
        """
        Finalize the audit log entry and write to file.

        Args:
            channel_name: The channel created (or None if skipped/failed)
            final_status: "success", "failure", "skipped", "dry_run"
        """
        total_duration = time.time() - self.start_time

        log_entry = {
            "run_id": self.run_id,
            "ticket_id": self.ticket_id,
            "ticket_type": self.ticket_type,
            "customer": self.customer,
            "priority": self.priority,
            "channel_name": channel_name,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "duration_seconds": round(total_duration, 2),
            "steps": self.steps,
            "summary": {
                "total_steps": len(self.steps),
                "steps_succeeded": sum(1 for s in self.steps if s["status"] == "success"),
                "steps_failed": sum(1 for s in self.steps if s["status"] == "failure"),
                "steps_timeout": sum(1 for s in self.steps if s["status"] == "timeout"),
                "final_status": final_status
            }
        }

        # Write to audit log
        self._write_log_entry(log_entry)

    def _write_log_entry(self, log_entry):
        """Write a log entry to the JSONL file."""
        # Ensure directory exists
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

        # Append entry as JSON line
        with open(self.log_path, "a") as f:
            f.write(json.dumps(log_entry) + "\n")

    def get_summary(self):
        """Get a human-readable summary of the log."""
        total_duration = time.time() - self.start_time
        succeeded = sum(1 for s in self.steps if s["status"] == "success")
        failed = sum(1 for s in self.steps if s["status"] == "failure")

        return f"Run {self.run_id}: {len(self.steps)} steps ({succeeded} succeeded, {failed} failed) in {total_duration:.1f}s"


# ============================================================================
# CONVENIENCE FUNCTIONS FOR COMMON AUDIT PATTERNS
# ============================================================================

def audit_preflight_checks(chrome_bridge_status, jira_status, slack_status, duration):
    """Format preflight check results."""
    return {
        "checks": {
            "chrome_bridge": chrome_bridge_status,  # "connected" or error message
            "jira_api": jira_status,                # "ok" or error message
            "slack_api": slack_status               # "ok" or error message
        }
    }


def audit_jira_fetch(ticket_id, customer, priority):
    """Format Jira metadata fetch results."""
    return {
        "details": {
            "ticket_id": ticket_id,
            "customer": customer,
            "priority": priority
        }
    }


def audit_channel_name(channel_name):
    """Format channel name generation results."""
    return {
        "details": {
            "channel_name": channel_name
        }
    }


def audit_slack_search(found, search_query, channel_name=None):
    """Format Slack search results."""
    details = {
        "search_query": search_query,
        "found": found
    }
    if channel_name:
        details["channel_name"] = channel_name
    return {"details": details}


def audit_browser_navigation(url, duration):
    """Format browser navigation action."""
    return {
        "action": "navigate",
        "url": url,
        "duration_seconds": duration
    }


def audit_browser_click(selector, found, duration, retries=0):
    """Format browser click action."""
    return {
        "action": "click",
        "selector": selector,
        "found": found,
        "duration_seconds": duration,
        "retries": retries
    }


def audit_browser_type(field, value, duration):
    """Format browser type action."""
    return {
        "action": "type",
        "field": field,
        "value_length": len(value),
        "duration_seconds": duration
    }


def audit_slack_verification(found, search_query, channel_name=None, attempts=1):
    """Format Slack verification results."""
    details = {
        "search_query": search_query,
        "found": found,
        "attempts": attempts
    }
    if channel_name:
        details["channel_name"] = channel_name
    return {"details": details}


def audit_message_post(channel_id, message_length, blocks_count=None):
    """Format message post results."""
    details = {
        "channel_id": channel_id,
        "message_length": message_length
    }
    if blocks_count is not None:
        details["blocks_count"] = blocks_count
    return {"details": details}


def audit_error(error_type, error_message, error_code=None, extra_details=None):
    """Format an error record."""
    error = {
        "type": error_type,
        "message": error_message
    }
    if error_code:
        error["code"] = error_code
    if extra_details:
        error.update(extra_details)
    return error


# ============================================================================
# DEMO/TESTING
# ============================================================================

if __name__ == "__main__":
    # Demo: Create a sample audit log
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "audit.log.jsonl"

        print("Creating sample audit log...")
        logger = AuditLogger("CASE-4009", "CASE", "ISOC", "P3", log_path=log_path)

        # Pre-flight checks
        logger.record_step(
            0, "Pre-flight Checks", "success", 2.1,
            details=audit_preflight_checks(
                "connected",
                "ok",
                "ok",
                2.1
            )
        )

        # Fetch Jira
        logger.record_step(
            1, "Fetch Jira metadata", "success", 1.5,
            details=audit_jira_fetch("CASE-4009", "ISOC", "P3")
        )

        # Generate name
        logger.record_step(
            2, "Generate channel name", "success", 0.1,
            details=audit_channel_name("case-4009-isoc-low")
        )

        # Check for existing
        logger.record_step(
            3, "Check for existing channel", "success", 0.8,
            details=audit_slack_search(False, "CASE-4009")
        )

        # Browser automation (with sub-actions)
        logger.record_action(4, "navigate", 8.0, {"url": "https://bloo-systems.atlassian.net/browse/CASE-4009"})
        logger.record_action(4, "find_trigger_button", 2.0, {"selector": "[data-testid=...]", "found": True})
        logger.record_action(4, "click_trigger", 1.0, {})
        logger.record_action(4, "locate_iframe", 2.0, {"pattern": "atlassian.wisoft.eu/slack/", "found": True})
        logger.record_action(4, "click_create_button", 1.0, {})
        logger.record_step(
            4, "Browser automation", "success", 28.0
        )

        # Verify in Slack
        logger.record_step(
            5, "Verify in Slack", "success", 2.0,
            details=audit_slack_verification(True, "CASE-4009", "case-4009-isoc-low")
        )

        # Audit log (instantaneous)
        logger.record_step(6, "Write audit log", "success", 0.05)

        # Post message
        logger.record_step(
            7, "Post starter message", "success", 1.5,
            details=audit_message_post("C1234567890", 245)
        )

        # Finalize
        logger.finalize("case-4009-isoc-low", "success")

        # Read and display the result
        print("\nAudit log entry:\n")
        with open(log_path) as f:
            entry = json.loads(f.read())
            print(json.dumps(entry, indent=2))

        print(f"\nLog file size: {log_path.stat().st_size} bytes")
        print(f"Summary: {logger.get_summary()}")
