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

Or run directly (JIRA_WEBHOOK_SECRET must be set, or pass --secret):
    python3 webhook_server.py --port 5000 --debug

Production (Gunicorn app factory):
    gunicorn -w 4 -b 0.0.0.0:5000 'webhook_server:create_app_from_env()'
"""

import json
import hmac
import hashlib
import html
import logging
import os
import re
import time
from typing import Dict, Any, Optional, Callable, List, Tuple
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
import asyncio
from collections import deque

from audit_logger import AuditLogger
from generate_channel_name import generate_channel_name, load_config as load_naming_config

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
    slack_invite_user_ids: tuple = ()


RETRY_STATUSES = {429, 502, 503, 504}
MAX_ATTEMPTS = 3
MAX_RETRY_WAIT_SECONDS = 30


def request_with_retry(method: str, url: str, idempotent: bool = False, sleep=time.sleep, **kwargs):
    """HTTP call with a timeout and at most MAX_ATTEMPTS tries.

    Rate limits (429) are always retried after Retry-After, because the server
    did not act on the request. 5xx and connection errors are retried only for
    idempotent calls: retrying a timed-out chat.postMessage could post twice.
    """
    kwargs.setdefault("timeout", 15)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.request(method, url, **kwargs)
        except (requests.ConnectionError, requests.Timeout):
            if not idempotent or attempt == MAX_ATTEMPTS:
                raise
            sleep(min(2 ** attempt, MAX_RETRY_WAIT_SECONDS))
            continue
        retryable = response.status_code == 429 or (idempotent and response.status_code in RETRY_STATUSES)
        if not retryable or attempt == MAX_ATTEMPTS:
            return response
        wait = response.headers.get("Retry-After")
        wait = float(wait) if wait and wait.replace(".", "", 1).isdigit() else 2 ** attempt
        logger.warning(f"{response.status_code} from {url.split('?')[0]}; retrying in {min(wait, MAX_RETRY_WAIT_SECONDS):.0f}s "
                       f"(attempt {attempt}/{MAX_ATTEMPTS})")
        sleep(min(wait, MAX_RETRY_WAIT_SECONDS))
    return response


class SlackMessenger:
    """Sends messages to Slack channels."""

    def __init__(self, token: str):
        if not HAS_REQUESTS:
            raise ImportError("requests is required. Install with: pip install requests")
        self.token = token
        self.base_url = "https://slack.com/api"

    def create_channel(self, channel_name: str) -> Dict[str, Any]:
        """Create a Slack channel."""
        data = {"name": channel_name.lower().replace(" ", "-")[:80], "is_private": False}
        try:
            result = self._call("conversations.create", data)
        except Exception as e:
            logger.error(f"Error creating channel: {e}")
            return {"success": False, "error": str(e)}
        if result.get("ok"):
            return {"success": True, "channel_id": result["channel"]["id"]}
        error = result.get("error", "Unknown error")
        if error == "name_taken":
            return {"success": True, "channel_id": None, "exists": True}
        logger.error(f"Failed to create channel: {error}")
        return {"success": False, "error": error}

    def send_message(self, channel_id: str, text: str, blocks: Optional[list] = None) -> bool:
        """Send a message to a Slack channel."""
        data = {"channel": channel_id, "text": text}
        if blocks:
            data["blocks"] = blocks
        try:
            result = self._call("chat.postMessage", data)
        except Exception as e:
            logger.error(f"Error sending message: {e}")
            return False
        if result.get("ok"):
            logger.info(f"Message sent to channel {channel_id}")
            return True
        logger.error(f"Failed to send message: {result.get('error')}")
        return False

    def _post(self, method: str, data: Dict[str, Any]) -> Dict[str, Any]:
        response = request_with_retry(
            "POST", f"{self.base_url}/{method}",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            json=data,
        )
        response.raise_for_status()
        return response.json()

    def _call(self, method: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Call a Slack method; if the bot has been removed from the channel, rejoin it and retry once."""
        result = self._post(method, data)
        if result.get("error") == "not_in_channel" and data.get("channel"):
            joined = self._post("conversations.join", {"channel": data["channel"]})
            if joined.get("ok"):
                logger.info(f"Rejoined channel {data['channel']} after not_in_channel on {method}")
                result = self._post(method, data)
            else:
                logger.error(f"Could not rejoin {data['channel']}: {joined.get('error')}")
        return result

    def post_message(self, channel_id: str, text: str, thread_ts: Optional[str] = None) -> Optional[str]:
        """Post a message (optionally as a thread reply); returns its ts, or None on failure."""
        data = {"channel": channel_id, "text": text, "unfurl_links": False}
        if thread_ts:
            data["thread_ts"] = thread_ts
        try:
            result = self._call("chat.postMessage", data)
        except Exception as e:
            logger.error(f"Error posting message: {e}")
            return None
        if not result.get("ok"):
            logger.error(f"Failed to post message to {channel_id}: {result.get('error')}")
            return None
        return result.get("ts")

    def set_topic(self, channel_id: str, topic: str) -> bool:
        try:
            result = self._call("conversations.setTopic", {"channel": channel_id, "topic": topic[:250]})
        except Exception as e:
            logger.error(f"Error setting topic: {e}")
            return False
        if not result.get("ok"):
            logger.error(f"Failed to set topic on {channel_id}: {result.get('error')}")
        return bool(result.get("ok"))

    def archive_channel(self, channel_id: str) -> bool:
        try:
            result = self._call("conversations.archive", {"channel": channel_id})
        except Exception as e:
            logger.error(f"Error archiving channel: {e}")
            return False
        if result.get("ok") or result.get("error") == "already_archived":
            return True
        logger.error(f"Failed to archive {channel_id}: {result.get('error')}")
        return False

    def invite_users(self, channel_id: str, user_ids: List[str]) -> bool:
        """Invite users to a channel."""
        try:
            result = self._call("conversations.invite", {"channel": channel_id, "users": ",".join(user_ids)})
        except Exception as e:
            logger.error(f"Error inviting users: {e}")
            return False
        if result.get("ok") or result.get("error") == "already_in_channel":
            logger.info(f"Invited {', '.join(user_ids)} to channel {channel_id}")
            return True
        logger.error(f"Failed to invite users: {result.get('error')}")
        return False


ORGANIZATIONS_FIELD = "customfield_10002"
PRODUCT_VERSION_FIELD = "customfield_10171"
PRODUCT_FIELD = "customfield_10194"
DESCRIPTION_MAX_CHARS = 500


def _option_value(field: Any) -> Optional[str]:
    if isinstance(field, dict):
        return field.get("value") or field.get("name")
    return field or None


GREETING_RE = re.compile(
    r"^(dear|hi|hello|hey|greetings|good\s+(morning|afternoon|evening))\b[^,.!:\n]{0,40}[,.!:]?\s*",
    re.IGNORECASE,
)
SIGN_OFF_RE = re.compile(
    r"^(thanks|thank\s+you|regards|best|kind|warm|warmest|sincerely|cheers|br)\b[\w\s&,.!-]{0,30}$",
    re.IGNORECASE,
)


def _adf_text(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    node_type = node.get("type")
    if node_type == "text":
        return node.get("text", "")
    if node_type == "hardBreak":
        return "\n"
    inner = "".join(_adf_text(child) for child in node.get("content", []))
    return inner + "\n" if node_type in ("paragraph", "heading", "listItem") else inner


def _strip_greeting_and_sign_off(lines: List[str]) -> List[str]:
    if lines:
        lines = [GREETING_RE.sub("", lines[0], count=1)] + lines[1:]
        if not lines[0]:
            lines = lines[1:]
    for i, line in enumerate(lines):
        if SIGN_OFF_RE.match(line):
            return lines[:i]
    return lines


def clean_description(description: Any) -> str:
    """Flatten a Jira description (plain/wiki text, HTML or ADF) into one short line, minus greeting and sign-off."""
    if isinstance(description, dict):
        text = _adf_text(description)
    else:
        text = re.sub(r"<br\s*/?>|</(p|div|li|h\d)>", "\n", description or "", flags=re.IGNORECASE)
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))

    lines = [" ".join(line.split()) for line in text.splitlines()]
    lines = [line for line in lines if line]
    body = _strip_greeting_and_sign_off(lines) or lines

    text = " ".join(body)
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
                "priority": (fields.get("priority") or {}).get("name"),
                "assignee": (fields.get("assignee") or {}).get("displayName", "Unassigned"),
                "summary": (fields.get("summary") or "").strip(),
                "organisation": ", ".join(organisations) or None,
                "customer": organisations[0] if organisations else None,
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

    def authenticated(self, payload: bytes, signature: str, token: str = "") -> bool:
        """Jira system webhooks sign the body (X-Hub-Signature); Automation rules can only send a static header."""
        if token and self.config.secret:
            return hmac.compare_digest(token, self.config.secret)
        return self.validator.validate_signature(payload, signature, self.config.secret)

    def handle_webhook(self, payload: bytes, signature: str, token: str = "") -> Dict[str, Any]:
        """
        Handle incoming webhook event.

        Returns:
            Response dict with status, message, and task details
        """
        try:
            # Validate signature
            if not self.authenticated(payload, signature, token):
                logger.warning("Invalid webhook signature")
                run = AuditLogger("unknown", None, None, None, source="webhook")
                run.record_step(0, "Validate signature", "failure", None,
                                error={"type": "invalid_signature", "message": "X-Hub-Signature did not match"})
                run.finalize(None, "rejected")
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
                "issue": event["issue"],
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


NAMING_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "config.yaml"
_naming_config: Optional[Dict[str, Any]] = None


def channel_name_for(ticket_data: Dict[str, Any]) -> Tuple[Optional[str], List[str]]:
    """Team-standard channel name (same rules as the skill), e.g. case-4009-isoc-low."""
    global _naming_config
    if _naming_config is None:
        _naming_config = load_naming_config(str(NAMING_CONFIG_PATH))
    return generate_channel_name(
        ticket_data.get("ticket_id"),
        ticket_data.get("project_key"),
        ticket_data.get("customer"),
        ticket_data.get("priority"),
        _naming_config,
    )


def provision_channel(messenger: "SlackMessenger", ticket_data: Dict[str, Any], invite_user_ids,
                      run: AuditLogger, starter_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Create the standard ticket channel, invite members and post the starter message; steps go on run."""
    # starter_data: backfills post the starter as the ticket was when opened; the name follows the current ticket.
    ticket_id = ticket_data.get("ticket_id")
    t = time.time()
    channel_name, errors = channel_name_for(ticket_data)
    if errors:
        reason = "; ".join(errors)
        logger.error(f"Cannot build channel name for {ticket_id}: {reason}")
        run.record_step(1, "Generate channel name", "failure", time.time() - t,
                        error={"type": "invalid_ticket_fields", "message": reason})
        return {"final_status": "failure", "channel_name": None, "channel_id": None}
    run.record_step(1, "Generate channel name", "success", time.time() - t, details={"channel_name": channel_name})
    logger.info(f"Syncing {ticket_id} to Slack channel {channel_name}")

    t = time.time()
    channel_result = messenger.create_channel(channel_name)
    if channel_result.get("exists"):
        logger.info(f"Channel {channel_name} already exists, avoiding channel duplication")
        run.record_step(2, "Create channel", "skipped", time.time() - t, details={"reason": "channel_already_exists"})
        return {"final_status": "skipped", "channel_name": channel_name, "channel_id": None}
    if not channel_result.get("success"):
        logger.error(f"Failed to create channel {channel_name}: {channel_result.get('error')}")
        run.record_step(2, "Create channel", "failure", time.time() - t,
                        error={"type": "slack_create_failed", "message": str(channel_result.get("error"))})
        return {"final_status": "failure", "channel_name": channel_name, "channel_id": None}
    channel_id = channel_result["channel_id"]
    run.record_step(2, "Create channel", "success", time.time() - t, details={"channel_id": channel_id})

    if invite_user_ids:
        t = time.time()
        invited = messenger.invite_users(channel_id, list(invite_user_ids))
        run.record_step(3, "Invite members", "success" if invited else "failure", time.time() - t,
                        details={"user_ids": list(invite_user_ids)})
    else:
        run.record_step(3, "Invite members", "skipped", 0, details={"reason": "SLACK_INVITE_USER_IDS not set"})

    t = time.time()
    message_data = format_starter_message(starter_data or ticket_data)
    sent = messenger.send_message(channel_id, message_data["text"], message_data["blocks"])
    run.record_step(4, "Post starter message", "success" if sent else "failure", time.time() - t)
    if sent:
        logger.info(f"Starter message sent to {channel_name}")
    else:
        logger.error(f"Failed to send starter message to {channel_name}")
    return {"final_status": "success" if sent else "failure", "channel_name": channel_name, "channel_id": channel_id}


def create_sync_callback(config: WebhookConfig) -> Optional[Callable]:
    """Return a callback that hands each Jira issue event to the shared sync engine (same logic as the poller)."""
    if not config.slack_token:
        logger.warning("Slack token not configured, sync callback disabled")
        return None

    from sync_engine import ChannelState, SyncEngine, default_state_path  # lazy: sync_engine imports this module
    import yaml

    sync_cfg = (yaml.safe_load(NAMING_CONFIG_PATH.read_text()).get("tier_2") or {}).get("sync") or {}
    engine = SyncEngine(SlackMessenger(config.slack_token), ChannelState(default_state_path()),
                        sync_cfg.get("archive_on_status", []), config.slack_invite_user_ids)

    def sync_callback(task: Dict[str, Any]) -> None:
        try:
            outcome = engine.process(task["issue"], source="webhook",
                                     is_new=task.get("event_type") == "jira:issue_created")
            logger.info(f"{task.get('ticket_id')}: {outcome}")
        except Exception as e:
            logger.error(f"Error in sync callback: {e}")
            run = AuditLogger(task.get("ticket_id") or "unknown", None, None, None, source="webhook")
            run.record_step(99, "Unexpected error", "failure", None, error={"type": type(e).__name__, "message": str(e)})
            run.finalize(None, "failure")

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
        token = request.headers.get("X-Webhook-Token", "")

        result = handler.handle_webhook(payload, signature, token)

        # Return appropriate status code
        if result["status"] == "queued":
            return jsonify(result), 200
        else:
            return jsonify(result), 400

    def local_only():
        # Requests arriving through ngrok (or any proxy) carry X-Forwarded-For; keep internals off the internet.
        return "X-Forwarded-For" not in request.headers

    @app.route("/webhooks/status", methods=["GET"])
    def webhook_status():
        """Get webhook handler status."""
        if not local_only():
            return jsonify({"status": "error", "message": "Not found"}), 404
        return jsonify(handler.get_status()), 200

    @app.route("/webhooks/health", methods=["GET"])
    def health_check():
        """Health check endpoint."""
        return jsonify({"status": "healthy"}), 200

    @app.route("/webhooks/queue", methods=["GET"])
    def get_queue():
        """Get queue status and next task (for testing)."""
        if not local_only():
            return jsonify({"status": "error", "message": "Not found"}), 404
        return jsonify({
            "queue_size": handler.queue.size(),
            "stats": handler.queue.stats(),
        }), 200

    return app


def config_from_env(secret: Optional[str] = None, **overrides) -> WebhookConfig:
    """Build config from the environment; refuses to run without a webhook secret."""
    secret = secret or os.environ.get("JIRA_WEBHOOK_SECRET")
    if not secret:
        raise RuntimeError(
            "JIRA_WEBHOOK_SECRET is not set. Refusing to start: without a secret, "
            "anyone who can reach this server could sign fake Jira events."
        )
    settings = {
        "debug": os.environ.get("DEBUG", "false").lower() == "true",
        "slack_token": os.environ.get("SLACK_BOT_TOKEN"),
        "slack_invite_user_ids": tuple(
            uid.strip() for uid in os.environ.get("SLACK_INVITE_USER_IDS", "").split(",") if uid.strip()
        ),
    }
    settings.update(overrides)
    return WebhookConfig(secret=secret, **settings)


def create_app_from_env():
    """Gunicorn app factory: gunicorn 'webhook_server:create_app_from_env()'."""
    config = config_from_env()
    return create_webhook_app(config, create_sync_callback(config))


def main():
    """Run webhook server."""
    import argparse

    parser = argparse.ArgumentParser(description="Jira webhook server for ticket sync")
    parser.add_argument("--port", type=int, default=5000, help="Port to listen on")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--secret", help="Jira webhook secret (default: $JIRA_WEBHOOK_SECRET)")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")

    args = parser.parse_args()

    overrides = {"port": args.port, "host": args.host}
    if args.debug:
        overrides["debug"] = True
    try:
        config = config_from_env(args.secret, **overrides)
    except RuntimeError as e:
        parser.error(str(e))

    app = create_webhook_app(config, create_sync_callback(config))

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
