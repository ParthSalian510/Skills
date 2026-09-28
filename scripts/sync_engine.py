#!/usr/bin/env python3
"""One place that decides what a Jira ticket change means for its Slack channel (used by the poller and the webhook)."""
import fcntl
import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from audit_logger import AuditLogger
from ticket_sync import StateChangeDetector
from webhook_server import WebhookValidator, provision_channel

SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_PATH = SKILL_ROOT / "state" / "channels.json"
TRACKED_FIELDS = ("status", "priority", "assignee")
FIELD_LABELS = {"status": "Status", "priority": "Priority", "assignee": "Assignee"}

logger = logging.getLogger("sync_engine")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_jira_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")


def default_state_path() -> Path:
    return Path(os.environ.get("CTC_STATE_PATH") or DEFAULT_STATE_PATH)


class ChannelState:
    """ticket -> channel mapping plus last-seen ticket fields, shared on disk by every process."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.started_at: str = ""
        self.last_poll_at: Optional[str] = None
        self.tickets: Dict[str, Dict[str, Any]] = {}
        with self.locked():
            pass

    def load(self) -> None:
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.started_at = data.get("started_at") or utcnow().isoformat()
        self.last_poll_at = data.get("last_poll_at")
        self.tickets = data.get("tickets", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"started_at": self.started_at, "last_poll_at": self.last_poll_at,
                                   "tickets": self.tickets}, indent=2))
        os.replace(tmp, self.path)

    @contextmanager
    def locked(self):
        """Exclusive read-modify-write across processes (gunicorn workers + poller)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                self.load()
                yield self
                self.save()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def snapshot(ticket: Dict[str, Any]) -> Dict[str, Any]:
    return {f: ticket.get(f) for f in TRACKED_FIELDS}


def format_update_message(ticket: Dict[str, Any], changes: Dict[str, Dict[str, Any]], archiving: bool) -> str:
    lines = [f"*Jira update · {ticket['ticket_id']}*"]
    for field, change in changes.items():
        lines.append(f"• {FIELD_LABELS[field]}: {change['old'] or 'Not set'} → {change['new'] or 'Not set'}")
    if archiving:
        lines.append("_Ticket resolved — archiving this channel._")
    if ticket.get("url"):
        lines.append(f"<{ticket['url']}|Open in Jira>")
    return "\n".join(lines)


class SyncEngine:
    def __init__(self, messenger, state: ChannelState, archive_statuses: List[str], invite_user_ids=()):
        self.messenger, self.state = messenger, state
        self.archive_statuses, self.invite_user_ids = archive_statuses, tuple(invite_user_ids)

    def process(self, issue: Dict[str, Any], source: str, is_new: Optional[bool] = None) -> str:
        """Apply one Jira issue snapshot. is_new=True (issue_created event) skips the created-after-start check."""
        ticket = WebhookValidator.extract_event_data({"webhookEvent": "jira:sync", "issue": issue})
        if not ticket or not ticket.get("ticket_id"):
            return "invalid"
        key = ticket["ticket_id"]
        with self.state.locked():
            tracked = self.state.tickets.get(key)
            if tracked is None:
                if not is_new:
                    created = parse_jira_time((issue.get("fields") or {}).get("created"))
                    if created is None or created < datetime.fromisoformat(self.state.started_at):
                        return "ignored"
                return self._provision(ticket, source)
            if tracked.get("archived") or not tracked.get("channel_id"):
                return "inactive"
            if "status" not in tracked:
                tracked.update(snapshot(ticket))
                return "baselined"
            changes = {f: {"old": tracked.get(f), "new": ticket.get(f)} for f in TRACKED_FIELDS
                       if tracked.get(f) != ticket.get(f)}
            if not changes:
                return "unchanged"
            return self._sync(ticket, tracked, changes, source)

    def backfill(self, issue: Dict[str, Any], changelog: List[Dict[str, Any]], comments: List[Dict[str, Any]],
                  source: str = "poller", dry_run: bool = False, pause: float = 1.1) -> Dict[str, Any]:
        """Create the channel for an already-progressed ticket and replay its full history into it."""
        ticket = WebhookValidator.extract_event_data({"webhookEvent": "jira:backfill", "issue": issue})
        key = ticket["ticket_id"]
        opening = {**ticket, **opening_snapshot(snapshot(ticket), changelog)}
        timeline = build_timeline(changelog, comments)
        internal = sum(1 for c in comments if c.get("jsdPublic") is False)
        created = parse_jira_time((issue.get("fields") or {}).get("created"))
        header = (f"*Ticket history · {key}* — replayed from Jira\n"
                  f"Opened {created.strftime('%d %b %Y %H:%M') if created else '?'} as *{opening.get('status')}* · "
                  f"now *{ticket.get('status')}* · {sum(e['kind'] == 'change' for e in timeline)} changes · "
                  f"{sum(e['kind'] == 'comment' for e in timeline)} public comments "
                  f"({internal} internal notes not included) · times as shown in Jira")
        plan = {"ticket_id": key, "opening": snapshot(opening), "current": snapshot(ticket), "header": header,
                "timeline": [format_event(e) for e in timeline]}
        if dry_run:
            return {"outcome": "dry_run", **plan}

        with self.state.locked():
            tracked = self.state.tickets.get(key)
            if tracked and tracked.get("channel_id"):
                return {"outcome": "already_tracked", "channel_id": tracked["channel_id"], **plan}
            run = self._run(ticket, source)
            result = provision_channel(self.messenger, ticket, self.invite_user_ids, run, starter_data=opening)
            if not result["channel_id"]:
                run.finalize(result["channel_name"], result["final_status"])
                return {"outcome": "existed" if result["final_status"] == "skipped" else "failed", **plan}
            channel_id = result["channel_id"]

            t = time.time()
            parent = self.messenger.post_message(channel_id, header)
            posted = 0
            for text in plan["timeline"]:
                if parent:
                    time.sleep(pause)  # chat.postMessage allows ~1 message/second per channel
                    posted += bool(self.messenger.post_message(channel_id, text, thread_ts=parent))
            replay_ok = bool(parent) and posted == len(plan["timeline"])
            run.record_step(5, "Replay history", "success" if replay_ok else "failure", time.time() - t,
                            details={"events": len(plan["timeline"]), "posted": posted, "internal_skipped": internal})

            t = time.time()
            topic = StateChangeDetector.format_topic(key, ticket.get("status") or "Unknown", ticket.get("priority") or "Not set")
            topic_ok = self.messenger.set_topic(channel_id, topic)
            current = snapshot(ticket)
            summary_ok = bool(self.messenger.post_message(channel_id, "*Synced to current state* · " + " · ".join(
                f"{FIELD_LABELS[f]}: {_slack_escape(current[f] or 'Not set')}" for f in TRACKED_FIELDS)))
            run.record_step(6, "Apply current state", "success" if topic_ok and summary_ok else "failure",
                            time.time() - t, details={"topic": topic})

            archived = False
            if StateChangeDetector.should_archive(ticket.get("status") or "", self.archive_statuses):
                archived = self.messenger.archive_channel(channel_id)
                run.record_step(7, "Archive channel", "success" if archived else "failure", None)
            self.state.tickets[key] = {"channel_id": channel_id, "channel_name": result["channel_name"],
                                       "archived": archived, **current}
            ok = result["final_status"] == "success" and replay_ok and topic_ok and summary_ok
            run.finalize(result["channel_name"], "success" if ok else "failure")
            return {"outcome": "backfilled" if ok else "partial", "channel_id": channel_id,
                    "channel_name": result["channel_name"], "posted": posted, "archived": archived, **plan}

    def _run(self, ticket: Dict[str, Any], source: str) -> AuditLogger:
        return AuditLogger(ticket["ticket_id"], ticket.get("project_key"), ticket.get("customer"),
                           ticket.get("priority"), source=source)

    def _provision(self, ticket: Dict[str, Any], source: str) -> str:
        run = self._run(ticket, source)
        result = provision_channel(self.messenger, ticket, self.invite_user_ids, run)
        run.finalize(result["channel_name"], result["final_status"])
        if result["channel_id"] or result["final_status"] == "skipped":
            # Remember already-existing channels too, so they aren't retried on every event.
            self.state.tickets[ticket["ticket_id"]] = {"channel_id": result["channel_id"],
                                                       "channel_name": result["channel_name"],
                                                       "archived": False, **snapshot(ticket)}
        return {"success": "created", "skipped": "existed"}.get(result["final_status"], "failed")

    def _sync(self, ticket, tracked, changes, source) -> str:
        key, channel_id = ticket["ticket_id"], tracked["channel_id"]
        run = self._run(ticket, source)
        run.record_step(1, "Detect changes", "success", None, details={"changes": changes})
        ok = True
        if "status" in changes or "priority" in changes:
            t = time.time()
            topic = StateChangeDetector.format_topic(key, ticket.get("status") or "Unknown",
                                                     ticket.get("priority") or "Not set")
            done = self.messenger.set_topic(channel_id, topic)
            ok &= done
            run.record_step(2, "Update topic", "success" if done else "failure", time.time() - t,
                            details={"topic": topic})
        archiving = "status" in changes and StateChangeDetector.should_archive(ticket.get("status") or "",
                                                                               self.archive_statuses)
        t = time.time()
        sent = self.messenger.send_message(channel_id, format_update_message(ticket, changes, archiving))
        ok &= sent
        run.record_step(3, "Post update", "success" if sent else "failure", time.time() - t)
        if archiving:
            t = time.time()
            archived = self.messenger.archive_channel(channel_id)
            ok &= archived
            tracked["archived"] = archived
            run.record_step(4, "Archive channel", "success" if archived else "failure", time.time() - t)
        tracked.update(snapshot(ticket))
        run.finalize(tracked.get("channel_name"), "success" if ok else "failure")
        logger.info(f"[{source}] synced {key}: {', '.join(changes)}{' + archived' if tracked.get('archived') else ''}")
        return "synced" if ok else "failed"


# ---------------------------------------------------------------- backfill (replay a ticket's whole history)

HISTORY_FIELDS = {"status": "status", "priority": "priority", "assignee": "assignee"}
COMMENT_MAX_CHARS = 2500


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def adf_to_text(node: Any) -> str:
    """Readable plain text from a Jira ADF comment body (mentions, links, lists, attachments)."""
    if not isinstance(node, dict):
        return ""
    kind, attrs = node.get("type"), node.get("attrs") or {}
    if kind == "text":
        return node.get("text", "")
    if kind == "hardBreak":
        return "\n"
    if kind == "mention":
        return attrs.get("text") or "@someone"
    if kind == "emoji":
        return attrs.get("text") or attrs.get("shortName", "")
    if kind in ("inlineCard", "blockCard"):
        return attrs.get("url", "")
    if kind in ("media", "mediaSingle", "mediaGroup"):
        inner = "".join(adf_to_text(c) for c in node.get("content", []))
        return inner if kind != "media" else "[attachment] "
    if kind == "tableRow":
        return " | ".join(adf_to_text(c).strip() for c in node.get("content", [])) + "\n"
    inner = "".join(adf_to_text(c) for c in node.get("content", []))
    if kind in ("tableCell", "tableHeader"):
        return " ".join(inner.split())
    if kind == "listItem":
        return "• " + inner.strip() + "\n"
    if kind in ("paragraph", "heading", "codeBlock", "blockquote", "bulletList", "orderedList"):
        return inner.rstrip("\n") + "\n"
    return inner


def clean_comment(body: Any) -> str:
    text = adf_to_text(body) if isinstance(body, dict) else str(body or "")
    lines = [" ".join(l.split()) for l in text.splitlines()]
    text = "\n".join(l for l in lines if l).strip()
    if len(text) > COMMENT_MAX_CHARS:
        text = text[:COMMENT_MAX_CHARS].rsplit(" ", 1)[0] + "…"
    return text or "[attachment]"


def _value(field: str, raw: Optional[str]) -> Optional[str]:
    return (raw or "Unassigned") if field == "assignee" else raw


def opening_snapshot(current: Dict[str, Any], changelog: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Undo every tracked-field change, newest first, to get the values the ticket was opened with."""
    state = dict(current)
    for entry in sorted(changelog, key=lambda e: e["created"], reverse=True):
        for item in entry.get("items", []):
            field = HISTORY_FIELDS.get(item.get("field"))
            if field:
                state[field] = _value(field, item.get("fromString"))
    return state


def build_timeline(changelog: List[Dict[str, Any]], comments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Chronological list of tracked-field changes and public comments (internal notes are dropped)."""
    events = []
    for entry in changelog:
        changes = [(HISTORY_FIELDS[i["field"]], _value(HISTORY_FIELDS[i["field"]], i.get("fromString")),
                    _value(HISTORY_FIELDS[i["field"]], i.get("toString")))
                   for i in entry.get("items", []) if i.get("field") in HISTORY_FIELDS]
        if changes:
            events.append({"at": parse_jira_time(entry["created"]), "kind": "change",
                           "who": (entry.get("author") or {}).get("displayName", "Jira"), "changes": changes})
    for c in comments:
        if c.get("jsdPublic") is False:
            continue
        events.append({"at": parse_jira_time(c["created"]), "kind": "comment",
                       "who": (c.get("author") or {}).get("displayName", "Unknown"), "text": clean_comment(c.get("body"))})
    return sorted(events, key=lambda e: e["at"])


def format_event(event: Dict[str, Any]) -> str:
    when = event["at"].strftime("%d %b %H:%M")
    who = _slack_escape(event["who"])
    if event["kind"] == "change":
        parts = "; ".join(f"{FIELD_LABELS[f]}: {_slack_escape(o or 'Not set')} → {_slack_escape(n or 'Not set')}"
                          for f, o, n in event["changes"])
        return f"*{when}* · {who} — {parts}"
    return f"*{when}* · :speech_balloon: *{who}*\n{_slack_escape(event['text'])}"
