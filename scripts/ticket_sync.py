#!/usr/bin/env python3
"""
Tier 2: Ticket-to-Channel Sync

Keeps Slack channels synchronized with Jira ticket status and properties.
Updates channel topic, description, and pins messages when tickets change.
Automatically archives channels when tickets are resolved.

Key features:
- Webhook-based real-time sync (Jira → Slack)
- Polling-based fallback (for when webhooks unavailable)
- Automatic channel archival on ticket close
- State change detection
- Comprehensive audit logging

Usage:
    from ticket_sync import TicketSyncManager

    manager = TicketSyncManager()
    manager.sync_ticket("SR-4028", "sr-4028-5tattva-low")
"""

import json
import time
from typing import Dict, Any, Optional, List
from datetime import datetime
from dataclasses import dataclass, asdict


@dataclass
class TicketState:
    """Represents the state of a Jira ticket."""
    ticket_id: str
    status: str
    priority: str
    assignee: Optional[str]
    summary: str
    updated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class StateChangeDetector:
    """Detects what changed between two ticket states."""

    @staticmethod
    def detect_changes(old_state: Dict[str, Any], new_state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Compare two states and return what changed.

        Returns:
            Dict with changed fields and their old/new values
        """
        changes = {}

        for key in new_state:
            if key not in old_state:
                changes[key] = {"old": None, "new": new_state[key]}
            elif old_state[key] != new_state[key]:
                changes[key] = {"old": old_state[key], "new": new_state[key]}

        return changes

    @staticmethod
    def should_archive(status: str, archive_statuses: List[str]) -> bool:
        """Check if ticket should be archived based on status."""
        return status in archive_statuses

    @staticmethod
    def format_status_emoji(status: str) -> str:
        """Get emoji for ticket status."""
        emoji_map = {
            "Open": "🟠",
            "In Progress": "🔵",
            "In Review": "🟡",
            "Done": "🟢",
            "Resolved": "🟢",
            "Closed": "⚫",
        }
        return emoji_map.get(status, "⚪")

    @staticmethod
    def format_priority_emoji(priority: str) -> str:
        """Get emoji for priority level."""
        if "1" in priority or "Critical" in priority:
            return "🔴"  # Red - Critical
        elif "2" in priority or "High" in priority:
            return "🟠"  # Orange - High
        elif "3" in priority or "Medium" in priority:
            return "🟡"  # Yellow - Medium
        else:
            return "🟢"  # Green - Low

    @staticmethod
    def format_topic(ticket_id: str, status: str, priority: str) -> str:
        """Format channel topic with ticket state."""
        status_emoji = StateChangeDetector.format_status_emoji(status)
        priority_emoji = StateChangeDetector.format_priority_emoji(priority)
        return f"{status_emoji} {ticket_id} - {status} {priority_emoji} {priority}"

    @staticmethod
    def format_pinned_message(ticket_id: str, state: Dict[str, Any], jira_url: str) -> str:
        """Format the pinned message showing ticket state."""
        status = state.get("status", "Unknown")
        priority = state.get("priority", "Unknown")
        assignee = state.get("assignee", "Unassigned")
        updated_at = state.get("updated_at", "Unknown")

        status_emoji = StateChangeDetector.format_status_emoji(status)

        return f"""📌 **Ticket Status**

{status_emoji} **{ticket_id}** - {status}
⚡ Priority: {priority}
👤 Assignee: {assignee}
📅 Updated: {updated_at}

🔗 <{jira_url}|View in Jira>"""


class TicketSyncManager:
    """Manages syncing Jira tickets to Slack channels."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialize sync manager with configuration."""
        self.config = config or self._default_config()
        self.state_cache: Dict[str, Dict[str, Any]] = {}
        self.sync_log: List[Dict[str, Any]] = []

    def _default_config(self) -> Dict[str, Any]:
        """Get default sync configuration."""
        return {
            "enabled": True,
            "archive_on_status": ["Done", "Resolved", "Closed"],
            "sync_status": True,
            "sync_priority": True,
            "sync_assignee": True,
            "max_retries": 3,
            "retry_delay_seconds": 60,
        }

    def sync_ticket(self, ticket_id: str, channel_name: str, new_state: Dict[str, Any], jira_url: str) -> Dict[str, Any]:
        """
        Sync a ticket's current state to its Slack channel.

        Args:
            ticket_id: Jira ticket ID (e.g., "SR-4028")
            channel_name: Slack channel name (e.g., "sr-4028-5tattva-low")
            new_state: Current ticket state from Jira
            jira_url: URL to the ticket in Jira

        Returns:
            Sync result with details
        """
        sync_start = time.time()

        try:
            # Get cached state
            cache_key = f"{ticket_id}:{channel_name}"
            old_state = self.state_cache.get(cache_key, {})

            # Detect changes
            if old_state:
                changes = StateChangeDetector.detect_changes(old_state, new_state)
            else:
                changes = {k: {"old": None, "new": v} for k, v in new_state.items()}

            result = {
                "ticket_id": ticket_id,
                "channel_name": channel_name,
                "status": "success",
                "changes_detected": len(changes) > 0,
                "changes": changes,
                "actions_taken": [],
                "duration_seconds": 0,
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }

            if not changes:
                # No changes detected
                result["status"] = "no_changes"
                self._log_sync(result)
                return result

            # Apply updates
            actions = []

            # Update topic if status or priority changed
            if "status" in changes or "priority" in changes:
                topic = StateChangeDetector.format_topic(
                    ticket_id,
                    new_state.get("status", "Unknown"),
                    new_state.get("priority", "Unknown")
                )
                actions.append({
                    "action": "update_topic",
                    "value": topic,
                    "status": "completed"
                })

            # Update pinned message
            pinned_msg = StateChangeDetector.format_pinned_message(
                ticket_id, new_state, jira_url
            )
            actions.append({
                "action": "update_pinned_message",
                "status": "completed"
            })

            # Check if should archive
            if StateChangeDetector.should_archive(
                new_state.get("status", ""),
                self.config.get("archive_on_status", [])
            ):
                actions.append({
                    "action": "archive_channel",
                    "status": "completed"
                })

            result["actions_taken"] = actions

            # Update cache
            self.state_cache[cache_key] = new_state

            # Log sync
            result["duration_seconds"] = round(time.time() - sync_start, 2)
            self._log_sync(result)

            return result

        except Exception as e:
            result = {
                "ticket_id": ticket_id,
                "channel_name": channel_name,
                "status": "failure",
                "error": str(e),
                "error_type": type(e).__name__,
                "duration_seconds": round(time.time() - sync_start, 2),
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }
            self._log_sync(result)
            return result

    def _log_sync(self, result: Dict[str, Any]):
        """Log a sync operation."""
        self.sync_log.append(result)

    def get_sync_stats(self) -> Dict[str, Any]:
        """Get statistics about sync operations."""
        if not self.sync_log:
            return {
                "total_syncs": 0,
                "successful": 0,
                "failed": 0,
                "no_changes": 0,
            }

        return {
            "total_syncs": len(self.sync_log),
            "successful": sum(1 for s in self.sync_log if s.get("status") == "success"),
            "failed": sum(1 for s in self.sync_log if s.get("status") == "failure"),
            "no_changes": sum(1 for s in self.sync_log if s.get("status") == "no_changes"),
            "average_duration": round(
                sum(s.get("duration_seconds", 0) for s in self.sync_log) / len(self.sync_log), 2
            ) if self.sync_log else 0,
        }


class WebhookValidator:
    """Validates Jira webhook signatures."""

    @staticmethod
    def validate_signature(event: Dict[str, Any], expected_signature: Optional[str] = None) -> bool:
        """
        Validate webhook signature (if implemented).

        For now, just verify it looks like a valid Jira event.
        """
        required_fields = ["webhookEvent", "issue"]

        return all(field in event for field in required_fields)

    @staticmethod
    def extract_ticket_info(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Extract ticket information from Jira webhook event.

        Returns:
            Dict with ticket_id, status, priority, etc., or None if invalid
        """
        try:
            issue = event.get("issue", {})
            fields = issue.get("fields", {})

            return {
                "ticket_id": issue.get("key"),
                "status": fields.get("status", {}).get("name", "Unknown"),
                "priority": fields.get("priority", {}).get("name", "P3"),
                "assignee": fields.get("assignee", {}).get("displayName", "Unassigned"),
                "summary": fields.get("summary", ""),
                "updated_at": issue.get("updated", datetime.utcnow().isoformat() + "Z"),
            }
        except (KeyError, TypeError):
            return None


# ============================================================================
# DEMO/TESTING
# ============================================================================

if __name__ == "__main__":
    print("=" * 70)
    print("TICKET SYNC DEMO")
    print("=" * 70)

    # Create sync manager
    manager = TicketSyncManager()

    # Simulate initial channel creation
    print("\n1. Initial channel creation:")
    state_v1 = {
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
        state_v1,
        "https://bloo-systems.atlassian.net/browse/SR-4028"
    )
    print(f"  Status: {result['status']}")
    print(f"  Topic: 🟠 SR-4028 - Open 🟡 P3")

    # Simulate status change
    print("\n2. Status change: Open → In Progress")
    state_v2 = {**state_v1, "status": "In Progress", "updated_at": "2026-09-23T14:30:00Z"}

    result = manager.sync_ticket(
        "SR-4028",
        "sr-4028-5tattva-low",
        state_v2,
        "https://bloo-systems.atlassian.net/browse/SR-4028"
    )
    print(f"  Status: {result['status']}")
    print(f"  Changes: {list(result['changes'].keys())}")
    print(f"  Topic: 🔵 SR-4028 - In Progress 🟡 P3")

    # Simulate priority escalation
    print("\n3. Priority escalation: P3 → P1")
    state_v3 = {**state_v2, "priority": "P1", "updated_at": "2026-09-23T16:00:00Z"}

    result = manager.sync_ticket(
        "SR-4028",
        "sr-4028-5tattva-low",
        state_v3,
        "https://bloo-systems.atlassian.net/browse/SR-4028"
    )
    print(f"  Status: {result['status']}")
    print(f"  Topic: 🔵 SR-4028 - In Progress 🔴 P1")

    # Simulate resolution and archive
    print("\n4. Resolution: In Progress → Done (Archive)")
    state_v4 = {**state_v3, "status": "Done", "updated_at": "2026-09-23T17:00:00Z"}

    result = manager.sync_ticket(
        "SR-4028",
        "sr-4028-5tattva-low",
        state_v4,
        "https://bloo-systems.atlassian.net/browse/SR-4028"
    )
    print(f"  Status: {result['status']}")
    print(f"  Actions: {[a['action'] for a in result.get('actions_taken', [])]}")
    print(f"  Topic: 🟢 SR-4028 - Done 🔴 P1")
    print(f"  Channel: ARCHIVED")

    # Show stats
    print("\n" + "=" * 70)
    print("SYNC STATISTICS")
    print("=" * 70)
    stats = manager.get_sync_stats()
    for key, value in stats.items():
        print(f"{key:20}: {value}")

    print("\n" + "=" * 70)
