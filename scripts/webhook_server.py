#!/usr/bin/env python3
"""
Webhook server for receiving Jira events and triggering ticket sync.

Receives webhook events from Jira when tickets are updated and syncs
the ticket status to corresponding Slack channels.

Usage:
    from webhook_server import create_webhook_app, WebhookConfig

    config = WebhookConfig(
        secret="jira-webhook-secret",
        debug=True
    )
    app = create_webhook_app(config)
    app.run(port=5000)

Or run directly:
    python3 webhook_server.py --port 5000 --debug
"""

import json
import hmac
import hashlib
import logging
import os
import re
from typing import Dict, Any, Optional, Callable, List
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
import asyncio
from collections import deque

try:
    from flask import Flask, request, jsonify
    HAS_FLASK = True
except ImportError:
    HAS_FLASK = False

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class WebhookConfig:
    """Configuration for webhook server."""
    secret: str
    debug: bool = False
    port: int = 5000
    host: str = "0.0.0.0"
    max_queue_size: int = 100
    slack_token: Optional[str] = None
    slack_channel_prefix: str = "ticket"
    slack_invite_user_ids: tuple = ()


class SlackMessenger:
    """Sends messages to Slack channels."""

    def __init__(self, token: str):
        if not HAS_REQUESTS:
            raise ImportError("requests is required. Install with: pip install requests")
        self.token = token
        self.base_url = "https://slack.com/api"

    def create_channel(self, channel_name: str) -> Dict[str, Any]:
        """Create a Slack channel."""
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        data = {
            "name": channel_name.lower().replace(" ", "-")[:80],
            "is_private": False,
        }
        try:
            response = requests.post(
                f"{self.base_url}/conversations.create",
                headers=headers,
                json=data,
            )
            response.raise_for_status()
            result = response.json()
            if result.get("ok"):
                return {"success": True, "channel_id": result["channel"]["id"]}
            else:
                error = result.get("error", "Unknown error")
                if error == "name_taken":
                    return {"success": True, "channel_id": None, "exists": True}
                logger.error(f"Failed to create channel: {error}")
                return {"success": False, "error": error}
        except Exception as e:
            logger.error(f"Error creating channel: {e}")
            return {"success": False, "error": str(e)}

    def send_message(self, channel_id: str, text: str, blocks: Optional[list] = None) -> bool:
        """Send a message to a Slack channel."""
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        data = {
            "channel": channel_id,
            "text": text,
        }
        if blocks:
            data["blocks"] = blocks

        try:
            response = requests.post(
                f"{self.base_url}/chat.postMessage",
                headers=headers,
                json=data,
            )
            response.raise_for_status()
            result = response.json()
            if result.get("ok"):
                logger.info(f"Message sent to channel {channel_id}")
                return True
            else:
                logger.error(f"Failed to send message: {result.get('error')}")
                return False
        except Exception as e:
            logger.error(f"Error sending message: {e}")
            return False

    def invite_users(self, channel_id: str, user_ids: List[str]) -> bool:
        """Invite users to a channel."""
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        data = {"channel": channel_id, "users": ",".join(user_ids)}
        try:
            response = requests.post(
                f"{self.base_url}/conversations.invite",
                headers=headers,
                json=data,
            )
            response.raise_for_status()
            result = response.json()
            if result.get("ok") or result.get("error") == "already_in_channel":
                logger.info(f"Invited {', '.join(user_ids)} to channel {channel_id}")
                return True
            logger.error(f"Failed to invite users: {result.get('error')}")
            return False
        except Exception as e:
            logger.error(f"Error inviting users: {e}")
            return False


ORGANIZATIONS_FIELD = "customfield_10002"
PRODUCT_VERSION_FIELD = "customfield_10171"
PRODUCT_FIELD = "customfield_10194"
DESCRIPTION_MAX_CHARS = 500


def _option_value(field: Any) -> Optional[str]:
    if isinstance(field, dict):
        return field.get("value") or field.get("name")
    return field or None


def _adf_text(node: Any) -> str:
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        return " ".join(_adf_text(child) for child in node.get("content", []))
    return ""


def clean_description(description: Any) -> str:
    """Flatten a Jira description (plain/wiki text, HTML or ADF) into one short line."""
    if isinstance(description, dict):
        text = _adf_text(description)
    else:
        text = re.sub(r"<[^>]+>", " ", description or "")
    text = " ".join(text.split())
    if len(text) > DESCRIPTION_MAX_CHARS:
        text = text[:DESCRIPTION_MAX_CHARS].rsplit(" ", 1)[0] + "…"
    return text or "No description provided"


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_starter_message(ticket_data: Dict[str, Any]) -> Dict[str, Any]:
    """Format the starter message posted when a ticket channel is created."""
    ticket_id = ticket_data.get("ticket_id", "UNKNOWN")
    esc = lambda key, default: _slack_escape(str(ticket_data.get(key) or default))

    lines = [
        f"*Organisation:* {esc('organisation', 'Not set')}",
        f"*Title:* {esc('summary', '')}",
        f"*Priority check:* {esc('priority', 'Not set')}",
        f"*Type Check:* {esc('issue_type', 'Unknown')} ({esc('project_key', '')})",
        f"*Product Version Check:* {esc('product_version', 'Not tracked in Jira for this ticket')}",
        f"*Description:* {esc('description', '')} Status: {esc('status', 'Unknown')}.",
    ]
    if ticket_data.get("url"):
        lines.append(f"\n*Jira:* <{ticket_data['url']}|{ticket_id}>")

    return {"text": "\n".join(lines), "blocks": None}


class WebhookValidator:
    """Validates Jira webhook signatures and extracts event data."""

    @staticmethod
    def validate_signature(payload: bytes, signature: str, secret: str) -> bool:
        """
        Validate webhook signature using HMAC-SHA256.

        Jira sends: X-Hub-Signature: sha256=<hash>
        We compute: sha256(secret + payload)
        """
        if not signature or not secret:
            return False

        # Extract hash from signature
        if not signature.startswith("sha256="):
            return False

        expected_hash = signature[7:]  # Remove "sha256=" prefix

        # Compute HMAC-SHA256
        computed = hmac.new(
            secret.encode(),
            payload,
            hashlib.sha256
        ).hexdigest()

        # Constant-time comparison to prevent timing attacks
        return hmac.compare_digest(computed, expected_hash)

    @staticmethod
    def extract_event_data(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Extract ticket information from Jira webhook event.

        Returns:
            Dict with ticket_id, status, priority, assignee, summary, updated_at
            or None if event is invalid
        """
        try:
            # Validate event structure
            if "webhookEvent" not in event or "issue" not in event:
                return None

            issue = event.get("issue") or {}
            fields = issue.get("fields") or {}
            ticket_id = issue.get("key")

            organisations = [o.get("name") for o in fields.get(ORGANIZATIONS_FIELD) or [] if o.get("name")]
            base_url = (issue.get("self") or "").split("/rest/")[0] or os.environ.get("JIRA_BASE_URL", "")

            return {
                "ticket_id": ticket_id,
                "status": (fields.get("status") or {}).get("name", "Unknown"),
                "priority": (fields.get("priority") or {}).get("name", "P3"),
                "assignee": (fields.get("assignee") or {}).get("displayName", "Unassigned"),
                "summary": (fields.get("summary") or "").strip(),
                "organisation": ", ".join(organisations) or None,
                "issue_type": (fields.get("issuetype") or {}).get("name"),
                "project_key": (fields.get("project") or {}).get("key") or (ticket_id or "").split("-")[0],
                "product_version": _option_value(fields.get(PRODUCT_VERSION_FIELD))
                or _option_value(fields.get(PRODUCT_FIELD)),
                "description": clean_description(fields.get("description")),
                "url": f"{base_url}/browse/{ticket_id}" if base_url and ticket_id else None,
                "updated_at": issue.get("updated", datetime.utcnow().isoformat() + "Z"),
                "event_type": event.get("webhookEvent"),
            }
        except (KeyError, TypeError, AttributeError):
            return None


class WebhookQueue:
    """In-memory queue for sync tasks."""

    def __init__(self, max_size: int = 100):
        self.queue = deque(maxlen=max_size)
        self.processed = 0
        self.failed = 0

    def enqueue(self, task: Dict[str, Any]) -> bool:
        """Add task to queue. Returns False if queue full."""
        try:
            self.queue.append(task)
            return True
        except Exception:
            return False

    def dequeue(self) -> Optional[Dict[str, Any]]:
        """Remove and return next task from queue."""
        try:
            return self.queue.popleft()
        except IndexError:
            return None

    def size(self) -> int:
        """Get current queue size."""
        return len(self.queue)

    def stats(self) -> Dict[str, int]:
        """Get queue statistics."""
        return {
            "queue_size": self.size(),
            "processed": self.processed,
            "failed": self.failed,
        }


class WebhookHandler:
    """Handles incoming webhook events and queues sync tasks."""

    def __init__(self, config: WebhookConfig, sync_callback: Optional[Callable] = None):
        self.config = config
        self.queue = WebhookQueue(config.max_queue_size)
        self.validator = WebhookValidator()
        self.sync_callback = sync_callback
        self.event_log: deque = deque(maxlen=100)

    def handle_webhook(self, payload: bytes, signature: str) -> Dict[str, Any]:
        """
        Handle incoming webhook event.

        Returns:
            Response dict with status, message, and task details
        """
        try:
            # Validate signature
            if not self.validator.validate_signature(payload, signature, self.config.secret):
                logger.warning("Invalid webhook signature")
                return {
                    "status": "error",
                    "message": "Invalid signature",
                    "code": "invalid_signature",
                }

            # Parse JSON
            event = json.loads(payload)
            logger.info(f"Webhook received: {event.get('webhookEvent')}")

            # Extract ticket data
            ticket_data = self.validator.extract_event_data(event)
            if not ticket_data:
                logger.warning("Could not extract ticket data from event")
                return {
                    "status": "error",
                    "message": "Could not extract ticket data",
                    "code": "invalid_event",
                }

            # Build sync task
            task = {
                "ticket_id": ticket_data["ticket_id"],
                "event_type": ticket_data["event_type"],
                "ticket_data": ticket_data,
                "received_at": datetime.utcnow().isoformat() + "Z",
                "status": "queued",
            }

            # Enqueue task
            if not self.queue.enqueue(task):
                logger.error("Queue full, dropping task")
                return {
                    "status": "error",
                    "message": "Queue full",
                    "code": "queue_full",
                }

            logger.info(f"Task queued for {task['ticket_id']}")

            # If sync callback provided, call it (async processing)
            if self.sync_callback:
                try:
                    self.sync_callback(task)
                except Exception as e:
                    logger.error(f"Error in sync callback: {e}")
                    # Don't fail the webhook response

            # Log event
            self.event_log.append({
                "ticket_id": task["ticket_id"],
                "event_type": task["event_type"],
                "timestamp": task["received_at"],
            })

            return {
                "status": "queued",
                "message": "Sync task queued",
                "ticket_id": task["ticket_id"],
                "event_type": task["event_type"],
            }

        except json.JSONDecodeError:
            logger.error("Invalid JSON payload")
            return {
                "status": "error",
                "message": "Invalid JSON",
                "code": "invalid_json",
            }
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            return {
                "status": "error",
                "message": str(e),
                "code": "internal_error",
            }

    def get_status(self) -> Dict[str, Any]:
        """Get webhook handler status."""
        return {
            "status": "operational",
            "queue": self.queue.stats(),
            "recent_events": list(self.event_log),
        }


def create_sync_callback(config: WebhookConfig) -> Optional[Callable]:
    """
    Create a sync callback function that sends messages to Slack.

    Args:
        config: WebhookConfig with slack_token

    Returns:
        Callback function or None if Slack is not configured
    """
    if not config.slack_token:
        logger.warning("Slack token not configured, sync callback disabled")
        return None

    messenger = SlackMessenger(config.slack_token)

    def sync_callback(task: Dict[str, Any]) -> None:
        """Process sync task: create channel and send starter message."""
        try:
            ticket_id = task.get("ticket_id")
            ticket_data = task.get("ticket_data", {})

            if not ticket_id:
                logger.error("No ticket_id in task")
                return

            channel_name = f"{config.slack_channel_prefix}-{ticket_id.lower()}"
            logger.info(f"Syncing {ticket_id} to Slack channel {channel_name}")

            channel_result = messenger.create_channel(channel_name)
            if not channel_result.get("success"):
                if not channel_result.get("exists"):
                    logger.error(f"Failed to create channel: {channel_result.get('error')}")
                    return

            channel_id = channel_result.get("channel_id")
            if not channel_id:
                logger.warning(f"Channel {channel_name} already exists, looking up ID")
                return

            if config.slack_invite_user_ids:
                messenger.invite_users(channel_id, list(config.slack_invite_user_ids))

            message_data = format_starter_message(ticket_data)
            success = messenger.send_message(
                channel_id,
                message_data["text"],
                message_data["blocks"],
            )

            if success:
                logger.info(f"Starter message sent to {channel_name}")
            else:
                logger.error(f"Failed to send starter message to {channel_name}")

        except Exception as e:
            logger.error(f"Error in sync callback: {e}")

    return sync_callback


def create_webhook_app(config: WebhookConfig, sync_callback: Optional[Callable] = None):
    """
    Create Flask app with webhook endpoints.

    Args:
        config: WebhookConfig instance
        sync_callback: Optional callback function to process queued tasks

    Returns:
        Flask app instance
    """
    if not HAS_FLASK:
        raise ImportError("Flask is required. Install with: pip install flask")

    app = Flask(__name__)
    app.config["DEBUG"] = config.debug

    handler = WebhookHandler(config, sync_callback)

    @app.route("/webhooks/jira", methods=["POST"])
    def jira_webhook():
        """Receive Jira webhook event."""
        payload = request.get_data()
        signature = request.headers.get("X-Hub-Signature", "")

        result = handler.handle_webhook(payload, signature)

        # Return appropriate status code
        if result["status"] == "queued":
            return jsonify(result), 200
        else:
            return jsonify(result), 400

    @app.route("/webhooks/status", methods=["GET"])
    def webhook_status():
        """Get webhook handler status."""
        return jsonify(handler.get_status()), 200

    @app.route("/webhooks/health", methods=["GET"])
    def health_check():
        """Health check endpoint."""
        return jsonify({"status": "healthy"}), 200

    @app.route("/webhooks/queue", methods=["GET"])
    def get_queue():
        """Get queue status and next task (for testing)."""
        return jsonify({
            "queue_size": handler.queue.size(),
            "stats": handler.queue.stats(),
        }), 200

    return app


# Create app for production deployment (Gunicorn)
config = WebhookConfig(
    secret=os.environ.get("JIRA_WEBHOOK_SECRET", "test-secret"),
    debug=os.environ.get("DEBUG", "false").lower() == "true",
    slack_token=os.environ.get("SLACK_BOT_TOKEN"),
    slack_channel_prefix=os.environ.get("SLACK_CHANNEL_PREFIX", "ticket"),
    slack_invite_user_ids=tuple(
        uid.strip() for uid in os.environ.get("SLACK_INVITE_USER_IDS", "").split(",") if uid.strip()
    ),
)
sync_callback = create_sync_callback(config)
app = create_webhook_app(config, sync_callback)


def main():
    """Run webhook server."""
    import argparse

    parser = argparse.ArgumentParser(description="Jira webhook server for ticket sync")
    parser.add_argument("--port", type=int, default=5000, help="Port to listen on")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--secret", default="test-secret", help="Jira webhook secret")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")

    args = parser.parse_args()

    config = WebhookConfig(
        secret=args.secret,
        port=args.port,
        host=args.host,
        debug=args.debug,
    )

    app = create_webhook_app(config)

    logger.info(f"Starting webhook server on {config.host}:{config.port}")
    logger.info(f"Endpoints:")
    logger.info(f"  POST   /webhooks/jira    - Receive Jira webhook")
    logger.info(f"  GET    /webhooks/status  - Get handler status")
    logger.info(f"  GET    /webhooks/health  - Health check")
    logger.info(f"  GET    /webhooks/queue   - Get queue status")

    if config.debug:
        logger.info("DEBUG MODE ENABLED")

    app.run(host=config.host, port=config.port, debug=config.debug)


if __name__ == "__main__":
    main()
