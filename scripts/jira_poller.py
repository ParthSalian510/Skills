#!/usr/bin/env python3
"""Poll Jira and keep ticket channels in sync: create for new tickets, update topic/notes on changes, archive on resolve."""
import argparse
import json
import logging
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_logger import AuditLogger
from sync_engine import ChannelState, SyncEngine, default_state_path, utcnow
from case_index import CaseIndex
from summarizer import from_config as summarizer_from_config
from webhook_server import SlackMessenger, request_with_retry

SKILL_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = SKILL_ROOT / "config" / "config.yaml"
JIRA_FIELDS = ["summary", "status", "priority", "assignee", "issuetype", "project", "description",
               "created", "updated", "resolutiondate", "customfield_10002", "customfield_10171", "customfield_10194"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("jira_poller")


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str):
        self.base_url = base_url.rstrip("/")
        self.auth = (email, api_token)

    def search(self, jql: str, fields: List[str]) -> List[Dict[str, Any]]:
        issues, page_token = [], None
        while True:
            body = {"jql": jql, "fields": fields, "maxResults": 100}
            if page_token:
                body["nextPageToken"] = page_token
            response = request_with_retry("POST", f"{self.base_url}/rest/api/3/search/jql", idempotent=True,
                                          json=body, auth=self.auth, headers={"Accept": "application/json"}, timeout=20)
            response.raise_for_status()
            data = response.json()
            issues.extend(data.get("issues", []))
            page_token = data.get("nextPageToken")
            if not page_token or data.get("isLast") is True:
                return issues

    def _get(self, path: str, **params) -> Dict[str, Any]:
        response = request_with_retry("GET", f"{self.base_url}/rest/api/3/{path}", idempotent=True, params=params,
                                      auth=self.auth, headers={"Accept": "application/json"}, timeout=20)
        response.raise_for_status()
        return response.json()

    def get_issue(self, key: str, fields: List[str]) -> Dict[str, Any]:
        return self._get(f"issue/{key}", fields=",".join(fields))

    def get_changelog(self, key: str) -> List[Dict[str, Any]]:
        entries, start = [], 0
        while True:
            page = self._get(f"issue/{key}/changelog", startAt=start, maxResults=100)
            entries.extend(page.get("values", []))
            start += len(page.get("values", []))
            if page.get("isLast", True) or not page.get("values"):
                return entries

    def get_comments(self, key: str) -> List[Dict[str, Any]]:
        comments, start = [], 0
        while True:
            page = self._get(f"issue/{key}/comment", startAt=start, maxResults=100, orderBy="created")
            comments.extend(page.get("comments", []))
            start += len(page.get("comments", []))
            if start >= page.get("total", 0) or not page.get("comments"):
                return comments


class Poller:
    def __init__(self, jira, messenger, state: ChannelState, projects: List[str], lookback_minutes: int,
                 archive_statuses: List[str], invite_user_ids=(), summarizer=None, summary_tickets=(),
                 case_index=None, index_channel_name="case-index", index_tickets=("*",)):
        self.jira, self.state = jira, state
        self.engine = SyncEngine(messenger, state, archive_statuses, invite_user_ids, summarizer=summarizer,
                                 fetch_comments=jira.get_comments if summarizer else None,
                                 summary_tickets=summary_tickets, case_index=case_index,
                                 index_channel_name=index_channel_name, index_tickets=index_tickets)
        self.projects, self.lookback_minutes = projects, lookback_minutes
        self.failing = False

    def lookback(self) -> int:
        """Minutes to look back: the configured window, widened to cover any gap since the last good poll."""
        if not self.state.last_poll_at:
            return self.lookback_minutes
        gap = (utcnow() - datetime.fromisoformat(self.state.last_poll_at)).total_seconds() / 60
        return max(self.lookback_minutes, math.ceil(gap) + 1)

    def jql(self) -> str:
        return f'project in ({", ".join(self.projects)}) AND updated >= "-{self.lookback()}m" ORDER BY updated ASC'

    def poll_once(self) -> Dict[str, int]:
        polled_at = utcnow().isoformat()
        with self.state.locked():
            jql = self.jql()
        issues = self.jira.search(jql, JIRA_FIELDS)
        counts: Dict[str, int] = {}
        for issue in issues:
            outcome = self.engine.process(issue, source="poller")
            counts[outcome] = counts.get(outcome, 0) + 1
        with self.state.locked():
            self.state.last_poll_at = polled_at
        return counts

    def run_forever(self, interval_seconds: int) -> None:
        logger.info(f"Polling {', '.join(self.projects)} every {interval_seconds}s")
        while True:
            start = time.time()
            try:
                counts = self.poll_once()
                if self.failing:
                    logger.info("Jira polling recovered")
                    self.failing = False
                active = {k: v for k, v in counts.items() if k not in ("unchanged", "ignored", "inactive")}
                if active:
                    logger.info(f"Poll: {active}")
            except Exception as e:
                logger.error(f"Poll failed: {e}")
                if not self.failing:
                    run = AuditLogger("poll", None, None, None, source="poller")
                    run.record_step(0, "Query Jira", "failure", time.time() - start,
                                    error={"type": type(e).__name__, "message": str(e)[:500]})
                    run.finalize(None, "failure")
                self.failing = True
            time.sleep(max(0.0, interval_seconds - (time.time() - start)))


def load_sync_config() -> Dict[str, Any]:
    config = yaml.safe_load(CONFIG_PATH.read_text())
    return {"site": config["jira"]["site"], "cloud_id": config["jira"]["cloud_id"],
            **config.get("tier_2", {}).get("sync", {})}


def jira_base_url(cfg: Dict[str, Any]) -> str:
    # Scoped API tokens only work through the api.atlassian.com gateway; JIRA_API_BASE overrides for classic tokens.
    return os.environ.get("JIRA_API_BASE") or f"https://api.atlassian.com/ex/jira/{cfg['cloud_id']}"


def build_poller(state_path: Path) -> "Poller":
    missing = [v for v in ("JIRA_EMAIL", "JIRA_API_TOKEN", "SLACK_BOT_TOKEN") if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"Missing environment variables: {', '.join(missing)}")
    cfg = load_sync_config()
    invite = [u.strip() for u in os.environ.get("SLACK_INVITE_USER_IDS", "").split(",") if u.strip()]
    return Poller(
        JiraClient(jira_base_url(cfg), os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"]),
        SlackMessenger(os.environ["SLACK_BOT_TOKEN"]),
        ChannelState(state_path),
        projects=cfg.get("polling_projects", ["CASE"]),
        lookback_minutes=int(cfg.get("polling_lookback_minutes", 2)),
        archive_statuses=cfg.get("archive_on_status", []),
        invite_user_ids=invite,
        summarizer=summarizer_from_config(cfg),
        summary_tickets=(cfg.get("summaries") or {}).get("tickets") or [],
        case_index=CaseIndex() if (cfg.get("case_index") or {}).get("enabled") else None,
        index_channel_name=(cfg.get("case_index") or {}).get("slack_channel", "case-index"),
        index_tickets=(cfg.get("case_index") or {}).get("tickets") or ["*"],
    )


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Keep Slack ticket channels in sync with Jira by polling.")
    p.add_argument("--state", default=str(default_state_path()))
    sub = p.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="poll forever")
    run_p.add_argument("--interval", type=int, help="seconds between polls (default: config)")
    sub.add_parser("once", help="poll once and exit")
    track = sub.add_parser("track", help="register an existing channel so its ticket gets synced")
    track.add_argument("ticket_id")
    track.add_argument("channel_id")
    track.add_argument("channel_name")
    sub.add_parser("status", help="show tracked tickets")
    bf = sub.add_parser("backfill", help="create the channel for an existing ticket and replay its whole history")
    bf.add_argument("ticket_id")
    bf.add_argument("--dry-run", action="store_true", help="print what would be posted; no Slack calls")
    sm = sub.add_parser("summarize", help="post a case summary so far into a tracked ticket's channel")
    sm.add_argument("ticket_id")
    sm.add_argument("--dry-run", action="store_true", help="print the summary; no Slack calls")
    ix = sub.add_parser("index", help="add closed tickets to the case index (#case-index + index/cases.jsonl)")
    ix.add_argument("ticket_ids", nargs="+")
    ix.add_argument("--dry-run", action="store_true", help="print the entries; write and post nothing")
    ic = sub.add_parser("index-channel", help="use an existing Slack channel as #case-index")
    ic.add_argument("channel_id")
    args = p.parse_args(argv)
    state_path = Path(args.state)

    if args.cmd == "track":
        state = ChannelState(state_path)
        with state.locked():
            state.tickets[args.ticket_id.upper()] = {"channel_id": args.channel_id,
                                                     "channel_name": args.channel_name, "archived": False}
        print(f"Tracking {args.ticket_id.upper()} → #{args.channel_name}; fields are baselined on the next poll.")
        return 0
    if args.cmd == "index-channel":
        state = ChannelState(state_path)
        with state.locked():
            state.meta["index_channel_id"] = args.channel_id
        print(f"Case index channel set to {args.channel_id}.")
        return 0
    if args.cmd == "status":
        state = ChannelState(state_path)
        print(json.dumps({"started_at": state.started_at, "last_poll_at": state.last_poll_at,
                          "meta": state.meta, "tickets": state.tickets}, indent=2))
        return 0

    poller = build_poller(state_path)
    if args.cmd == "backfill":
        key = args.ticket_id.upper()
        issue = poller.jira.get_issue(key, JIRA_FIELDS)
        result = poller.engine.backfill(issue, poller.jira.get_changelog(key), poller.jira.get_comments(key),
                                        dry_run=args.dry_run)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["outcome"] in ("dry_run", "backfilled") else 1
    if args.cmd == "summarize":
        if not poller.engine.summarizer:
            raise SystemExit("Summaries are off: set tier_2.sync.summaries.enabled in config/config.yaml")
        key = args.ticket_id.upper()
        result = poller.engine.summarize_so_far(poller.jira.get_issue(key, JIRA_FIELDS), poller.jira.get_comments(key),
                                                dry_run=args.dry_run)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["outcome"] in ("dry_run", "posted") else 1
    if args.cmd == "index":
        if not (poller.engine.summarizer and poller.engine.case_index is not None):
            raise SystemExit("Case index is off: enable tier_2.sync.summaries and tier_2.sync.case_index in config")
        results = []
        for key in (k.upper() for k in args.ticket_ids):
            issue = poller.jira.get_issue(key, JIRA_FIELDS)
            status = ((issue.get("fields") or {}).get("status") or {}).get("name")
            if status not in poller.engine.archive_statuses and not args.dry_run:
                results.append({"outcome": "not_closed", "ticket_id": key, "status": status})
                continue
            comments = poller.jira.get_comments(key)
            with poller.state.locked():
                tracked = poller.state.tickets.get(key)
                result = poller.engine.close_case(issue, comments, tracked=tracked, source="poller",
                                                  dry_run=args.dry_run)
            results.append(result)
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return 0 if all(r["outcome"] in ("indexed", "dry_run") for r in results) else 1
    if args.cmd == "once":
        print(json.dumps(poller.poll_once()))
        return 0
    poller.run_forever(args.interval or int(load_sync_config().get("polling_interval_seconds", 60)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
