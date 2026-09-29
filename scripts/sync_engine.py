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
        self.meta: Dict[str, Any] = {}  # e.g. index_channel_id
        with self.locked():
            pass

    def load(self) -> None:
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.started_at = data.get("started_at") or utcnow().isoformat()
        self.last_poll_at = data.get("last_poll_at")
        self.tickets = data.get("tickets", {})
        self.meta = data.get("meta", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"started_at": self.started_at, "last_poll_at": self.last_poll_at,
                                   "meta": self.meta, "tickets": self.tickets}, indent=2))
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
    def __init__(self, messenger, state: ChannelState, archive_statuses: List[str], invite_user_ids=(),
                 summarizer=None, fetch_comments=None, summary_tickets=(), case_index=None,
                 index_channel_name: str = "case-index", index_tickets=("*",), similar_cases: bool = True):
        """summarizer + fetch_comments(key) turn on comment summaries for summary_tickets ("*" = all tracked).
        With case_index too, closing a ticket in index_tickets posts a resolution summary and indexes the case."""
        self.messenger, self.state = messenger, state
        self.archive_statuses, self.invite_user_ids = archive_statuses, tuple(invite_user_ids)
        self.summarizer, self.fetch_comments = summarizer, fetch_comments
        self.summary_tickets = {k.upper() for k in summary_tickets}
        self.case_index, self.index_channel_name = case_index, index_channel_name
        self.index_tickets = {k.upper() for k in index_tickets}
        self.similar_cases = similar_cases

    @property
    def similar_on_new(self) -> bool:
        return bool(self.summarizer and self.case_index is not None and self.similar_cases)

    def post_similar_cases(self, ticket: Dict[str, Any], channel_id: str) -> str:
        """Post up to 3 related past cases (checked by Claude) in a new ticket's channel.

        Returns "posted", "none" (no good match, nothing posted) or "failed". Other customers' names are
        left out on purpose: a ticket channel may one day be shared with that customer.
        """
        from case_index import similar
        candidates = similar(self.case_index, f"{ticket.get('summary')}\n{ticket.get('description')}",
                             exclude=ticket.get("ticket_id") or "")
        if not candidates:
            return "none"
        picks = self.summarizer.pick_related(ticket, candidates)
        if picks is None:
            return "failed"
        if not picks:
            return "none"
        by_id = {c["ticket_id"]: c for c in candidates}
        lines = ["*Possibly related past cases* · picked from #case-index, check before relying on them"]
        for p in picks:
            c = by_id[p["ticket_id"]]
            ref = f"<{c['jira_url']}|{c['ticket_id']}>" if c.get("jira_url") else c["ticket_id"]
            fix = (c.get("fix") or "Not recorded").strip()
            fix = fix if len(fix) <= 220 else fix[:220].rsplit(" ", 1)[0] + "…"
            lines.append(f"• {ref} · closed {(c.get('closed') or '?')[:10]} — {_slack_escape(p['why'] or c.get('title') or '')}\n"
                         f"   _Fix:_ {_slack_escape(fix)}")
        return "posted" if self.messenger.post_message(channel_id, "\n".join(lines)) else "failed"

    def indexing_for(self, key: str) -> bool:
        return bool(self.summarizer and self.fetch_comments and self.case_index is not None and
                    ("*" in self.index_tickets or key.upper() in self.index_tickets))

    def summaries_for(self, key: str) -> bool:
        return bool(self.summarizer and self.fetch_comments and
                    ("*" in self.summary_tickets or key.upper() in self.summary_tickets))

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
                if self.summaries_for(key):
                    self._sync_comments(ticket, tracked, source)  # first sight: only records the cursor
                return "baselined"
            changes = {f: {"old": tracked.get(f), "new": ticket.get(f)} for f in TRACKED_FIELDS
                       if tracked.get(f) != ticket.get(f)}
            result = self._sync(ticket, tracked, changes, source, issue=issue) if changes else "unchanged"
            if self.summaries_for(key) and not tracked.get("archived"):
                comment_result = self._sync_comments(ticket, tracked, source)
                if comment_result and result == "unchanged":
                    result = comment_result
            return result

    def _sync_comments(self, ticket: Dict[str, Any], tracked: Dict[str, Any], source: str) -> Optional[str]:
        """Post a summary of public comments added since the last one seen. Caller holds the state lock.

        The first time a ticket is seen, only the cursor (highest comment id) is recorded, so switching this
        on never floods a channel with old history. The cursor always advances, even when the summary fails,
        so a broken summarizer can't re-post the same comments every minute.
        """
        key, channel_id = ticket["ticket_id"], tracked["channel_id"]
        comments = self.fetch_comments(key)
        newest = max((int(c["id"]) for c in comments if str(c.get("id", "")).isdigit()), default=0)
        if "last_comment_id" not in tracked:
            tracked["last_comment_id"] = newest
            return None
        last = int(tracked["last_comment_id"])
        public = [c for c in comments if c.get("jsdPublic") is not False and str(c.get("id", "")).isdigit()]
        new = [c for c in public if int(c["id"]) > last]
        tracked["last_comment_id"] = max(newest, last)
        if not new:
            return None
        run = self._run(ticket, source)
        t = time.time()
        body = self.summarizer.update(ticket, new, [c for c in public if int(c["id"]) <= last], clean_comment)
        n = f"{len(new)} new public comment{'s' if len(new) != 1 else ''}"
        if body == "":
            run.record_step(1, "Summarise comments", "skipped", time.time() - t,
                            details={"comments": len(new), "reason": "nothing of substance"})
            run.finalize(tracked.get("channel_name"), "skipped")
            return "comments_skipped"
        run.record_step(1, "Summarise comments", "success" if body else "failure", time.time() - t,
                        details={"comments": len(new)})
        text = (f"*Update · {key}* · {n}\n{body}" if body else
                f"*Update · {key}* · {n}. Summary unavailable right now, see Jira.")
        if ticket.get("url"):
            text += f"\n<{ticket['url']}|Open in Jira>"
        posted = bool(self.messenger.post_message(channel_id, text))
        run.record_step(2, "Post summary", "success" if posted else "failure", None)
        run.finalize(tracked.get("channel_name"), "success" if posted and body else "failure")
        logger.info(f"[{source}] summarised {n} on {key}")
        return "summarized" if posted and body else "failed"

    # ------------------------------------------------------------ case index

    def index_channel_id(self) -> Optional[str]:
        """#case-index, created (and people invited) on first use; its id is kept in state."""
        cid = self.state.meta.get("index_channel_id")
        if cid:
            return cid
        created = self.messenger.create_channel(self.index_channel_name)
        cid = created.get("channel_id")
        if not cid:
            logger.error(f"Could not create #{self.index_channel_name}: {created.get('error') or 'name taken'}; "
                         f"set it with: jira_poller.py index-channel <CHANNEL_ID>")
            return None
        if self.invite_user_ids:
            self.messenger.invite_users(cid, list(self.invite_user_ids))
        self.messenger.set_topic(cid, "Closed cases: problem, root cause and fix. One message per case; "
                                      "search here or with scripts/case_index.py.")
        self.state.meta["index_channel_id"] = cid
        return cid

    def build_index_entry(self, issue: Dict[str, Any], comments: List[Dict[str, Any]],
                          tracked: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        ticket = WebhookValidator.extract_event_data({"webhookEvent": "jira:index", "issue": issue})
        public = [c for c in comments if c.get("jsdPublic") is not False]
        res = self.summarizer.resolution(ticket, public, clean_comment)
        if not res:
            return None
        fields = issue.get("fields") or {}
        from summarizer import scrub
        return {"ticket_id": ticket["ticket_id"], "title": scrub(ticket.get("summary") or ""),
                "customer": ticket.get("customer"), "product_version": ticket.get("product_version"),
                "priority": ticket.get("priority"), "status": ticket.get("status"),
                "opened": fields.get("created"), "closed": fields.get("resolutiondate") or fields.get("updated"),
                **res, "jira_url": ticket.get("url"),
                "slack_channel_id": (tracked or {}).get("channel_id"),
                "slack_channel_name": (tracked or {}).get("channel_name"),
                "public_comments": len(public), "indexed_at": utcnow().isoformat()}

    def close_case(self, issue: Dict[str, Any], comments: List[Dict[str, Any]], tracked: Optional[Dict[str, Any]] = None,
                   source: str = "poller", dry_run: bool = False) -> Dict[str, Any]:
        """Resolution summary → index file + #case-index (+ the ticket's own channel if it's still open).

        Caller holds the state lock when tracked is given. Nothing is posted if the summary failed:
        the ticket can be indexed later with `jira_poller.py index KEY`.
        """
        entry = self.build_index_entry(issue, comments, tracked)
        key = issue.get("key")
        if not entry:
            logger.error(f"[{source}] resolution summary failed for {key}; not indexed")
            return {"outcome": "failed", "ticket_id": key}
        if dry_run:
            return {"outcome": "dry_run", "ticket_id": key, "entry": entry,
                    "message": format_index_message(entry)}
        replaced = self.case_index.upsert(entry)
        try:  # keep the Markdown pages (index/pages/) in step with the index; never blocks indexing
            from case_index import export_pages
            export_pages(self.case_index, self.case_index.path.parent / "pages")
        except Exception as e:
            logger.error(f"Case pages not refreshed: {e}")
        posted_index = posted_channel = False
        cid = self.index_channel_id()
        if cid:
            posted_index = bool(self.messenger.post_message(cid, format_index_message(entry)))
        if tracked and tracked.get("channel_id") and not tracked.get("archived"):
            posted_channel = bool(self.messenger.post_message(tracked["channel_id"],
                                                              format_index_message(entry, in_channel=True)))
        if tracked is not None:
            tracked["indexed"] = True
        logger.info(f"[{source}] indexed {key}{' (replaced)' if replaced else ''}")
        return {"outcome": "indexed", "ticket_id": key, "replaced": replaced, "posted_index": posted_index,
                "posted_channel": posted_channel, "entry": entry}

    def summarize_so_far(self, issue: Dict[str, Any], comments: List[Dict[str, Any]],
                         dry_run: bool = False) -> Dict[str, Any]:
        """Post a one-off case summary into a tracked ticket's channel and start comment summaries from here."""
        ticket = WebhookValidator.extract_event_data({"webhookEvent": "jira:summary", "issue": issue})
        key = ticket["ticket_id"]
        public = [c for c in comments if c.get("jsdPublic") is not False]
        summary = self.summarizer.case_summary(ticket, public, clean_comment) if self.summarizer else None
        text = case_summary_message(ticket, summary, len(public), len(comments) - len(public), so_far=True)
        if dry_run:
            return {"outcome": "dry_run", "ticket_id": key, "message": text}
        with self.state.locked():
            tracked = self.state.tickets.get(key)
            if not tracked or not tracked.get("channel_id"):
                return {"outcome": "not_tracked", "ticket_id": key, "message": text}
            if summary is None:
                return {"outcome": "failed", "ticket_id": key, "message": text}
            posted = bool(self.messenger.post_message(tracked["channel_id"], text))
            if posted:
                tracked["last_comment_id"] = max((int(c["id"]) for c in comments if str(c.get("id", "")).isdigit()),
                                                 default=int(tracked.get("last_comment_id") or 0))
            return {"outcome": "posted" if posted else "failed", "ticket_id": key,
                    "channel_id": tracked["channel_id"], "message": text}

    def backfill(self, issue: Dict[str, Any], changelog: List[Dict[str, Any]], comments: List[Dict[str, Any]],
                  source: str = "poller", dry_run: bool = False, pause: float = 1.1) -> Dict[str, Any]:
        """Create the channel for an already-progressed ticket and replay its full history into it."""
        ticket = WebhookValidator.extract_event_data({"webhookEvent": "jira:backfill", "issue": issue})
        key = ticket["ticket_id"]
        created = parse_jira_time((issue.get("fields") or {}).get("created"))
        opening = {**ticket, **opening_snapshot(snapshot(ticket), changelog, created)}
        timeline = build_timeline(changelog, comments, created)
        internal = sum(1 for c in comments if c.get("jsdPublic") is False)
        header = (f"*Ticket history · {key}* — replayed from Jira\n"
                  f"Opened {created.strftime('%d %b %Y %H:%M') if created else '?'} as *{opening.get('status')}* · "
                  f"now *{ticket.get('status')}* · {sum(e['kind'] == 'change' for e in timeline)} changes · "
                  f"{sum(e['kind'] == 'comment' for e in timeline)} public comments "
                  f"({internal} internal notes not included) · times as shown in Jira")
        if self.summarizer:
            public = [c for c in comments if c.get("jsdPublic") is not False]
            summary = self.summarizer.case_summary(ticket, public, clean_comment)
            header = case_summary_message(ticket, summary, len(public), internal)
            timeline = [e for e in timeline if e["kind"] == "change"]  # comments live in the summary, not copied
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
                                       "archived": archived, **current,
                                       "last_comment_id": max((int(c["id"]) for c in comments
                                                               if str(c.get("id", "")).isdigit()), default=0)}
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
        if result["channel_id"] and self.similar_on_new:
            t = time.time()
            outcome = self.post_similar_cases(ticket, result["channel_id"])
            run.record_step(5, "Similar past cases", "success" if outcome in ("posted", "none") else "failure",
                            time.time() - t, details={"outcome": outcome})
        run.finalize(result["channel_name"], result["final_status"])
        if result["channel_id"] or result["final_status"] == "skipped":
            # Remember already-existing channels too, so they aren't retried on every event.
            self.state.tickets[ticket["ticket_id"]] = {"channel_id": result["channel_id"],
                                                       "channel_name": result["channel_name"],
                                                       "archived": False, **snapshot(ticket)}
        return {"success": "created", "skipped": "existed"}.get(result["final_status"], "failed")

    def _sync(self, ticket, tracked, changes, source, issue: Optional[Dict[str, Any]] = None) -> str:
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
        if archiving and self.indexing_for(key):
            # Last chance to write in the channel: archived channels can't be posted to.
            t = time.time()
            outcome = self.close_case(issue or {}, self.fetch_comments(key), tracked=tracked, source=source)
            run.record_step(3.5, "Resolution summary + case index", "success" if outcome["outcome"] == "indexed" else "failure",
                            time.time() - t, details={"outcome": outcome["outcome"]})
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


# Jira Automation rules often adjust a ticket in its first seconds (CASE-4009: P1 → P3
# three seconds after creation). Those changes are part of how the ticket was opened,
# so the starter message shows the settled values and the history doesn't list them.
OPENING_SETTLE_SECONDS = 60


def is_opening_automation(entry: Dict[str, Any], created: Optional[datetime]) -> bool:
    """A change made by an app account (e.g. "Automation for Jira") within the settle window after creation."""
    if not created or (entry.get("author") or {}).get("accountType") != "app":
        return False
    at = parse_jira_time(entry.get("created"))
    return at is not None and 0 <= (at - created).total_seconds() <= OPENING_SETTLE_SECONDS


def opening_snapshot(current: Dict[str, Any], changelog: List[Dict[str, Any]],
                     created: Optional[datetime] = None) -> Dict[str, Any]:
    """Undo tracked-field changes, newest first, to get the values the ticket was opened with.

    With `created`, Automation's changes in the first OPENING_SETTLE_SECONDS are kept
    (not undone): the opening state is what the team first saw, not the raw form input.
    """
    state = dict(current)
    for entry in sorted(changelog, key=lambda e: e["created"], reverse=True):
        if is_opening_automation(entry, created):
            continue
        for item in entry.get("items", []):
            field = HISTORY_FIELDS.get(item.get("field"))
            if field:
                state[field] = _value(field, item.get("fromString"))
    return state


def build_timeline(changelog: List[Dict[str, Any]], comments: List[Dict[str, Any]],
                   created: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Chronological list of tracked-field changes and public comments (internal notes are dropped).

    With `created`, Automation's opening adjustments are left out: they're already in the opening state.
    """
    events = []
    for entry in changelog:
        if is_opening_automation(entry, created):
            continue
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


def format_index_message(entry: Dict[str, Any], in_channel: bool = False) -> str:
    """#case-index message (or the closing note in the ticket's own channel)."""
    key = entry["ticket_id"]
    head = (f"*Resolution summary · {key}*" if in_channel else
            f"*{key}* · {_slack_escape(entry.get('customer') or '?')} · "
            f"{_slack_escape(entry.get('product_version') or 'no version')} · {entry.get('priority') or '?'}"
            f" — {_slack_escape(entry.get('title') or '')}")
    lines = [head,
             f"*Problem:* {_slack_escape(entry.get('problem') or '-')}",
             f"*Root cause:* {_slack_escape(entry.get('root_cause') or '-')}",
             f"*Fix:* {_slack_escape(entry.get('fix') or '-')}"]
    tail = []
    if entry.get("components"):
        tail.append("_" + _slack_escape(", ".join(entry["components"])) + "_")
    if entry.get("jira_url"):
        tail.append(f"<{entry['jira_url']}|Open in Jira>")
    if not in_channel and entry.get("slack_channel_id"):
        tail.append(f"<#{entry['slack_channel_id']}>")
    if tail:
        lines.append(" · ".join(tail))
    return "\n".join(lines)


def case_summary_message(ticket: Dict[str, Any], summary: Optional[str], public: int, internal: int,
                         so_far: bool = False) -> str:
    """Parent message for a case summary. Without a summary (the call failed) it says so instead of copying comments."""
    key = ticket.get("ticket_id")
    title = f"*Case summary{' so far' if so_far else ''} · {key}*"
    source = (f"from {public} public comment{'s' if public != 1 else ''}"
              + (f" ({internal} internal notes not included)" if internal else ""))
    body = summary if summary else "Summary unavailable right now, see Jira for the comments."
    text = f"{title} · {source}\n{body}"
    if ticket.get("url"):
        text += f"\n<{ticket['url']}|Open in Jira>"
    return text


def format_event(event: Dict[str, Any]) -> str:
    when = event["at"].strftime("%d %b %H:%M")
    who = _slack_escape(event["who"])
    if event["kind"] == "change":
        parts = "; ".join(f"{FIELD_LABELS[f]}: {_slack_escape(o or 'Not set')} → {_slack_escape(n or 'Not set')}"
                          for f, o, n in event["changes"])
        return f"*{when}* · {who} — {parts}"
    return f"*{when}* · :speech_balloon: *{who}*\n{_slack_escape(event['text'])}"
