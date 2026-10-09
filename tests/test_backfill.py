#!/usr/bin/env python3
"""Tests for replaying a ticket's full history into its channel. Run: python3 tests/test_backfill.py"""
import sys
import os
os.environ.setdefault("CTC_RUN_MODE", "test")
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import sync_engine as se

passed = 0
failed = 0


def check(label, condition, details=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"✓ {label}")
    else:
        failed += 1
        print(f"✗ {label}")
        if details:
            print(f"  {details}")


def para(*nodes):
    return {"type": "paragraph", "content": list(nodes)}


def txt(t):
    return {"type": "text", "text": t}


doc = {"type": "doc", "content": [
    para(txt("Hello "), {"type": "mention", "attrs": {"text": "@Sam"}}, {"type": "hardBreak"}, txt("see "),
         {"type": "inlineCard", "attrs": {"url": "https://zoom.us/j/1"}}),
    {"type": "mediaGroup", "content": [{"type": "media"}, {"type": "media"}]},
    {"type": "bulletList", "content": [{"type": "listItem", "content": [para(txt("Used: 132 GB"))]}]},
    {"type": "table", "content": [{"type": "tableRow", "content": [
        {"type": "tableHeader", "content": [para(txt("Field"))]},
        {"type": "tableCell", "content": [para(txt("Details"))]}]}]},
]}
text = se.clean_comment(doc)
check("Mention rendered", "@Sam" in text, text)
check("Link rendered", "https://zoom.us/j/1" in text)
check("Attachments separated", "[attachment] [attachment]" in text, text)
check("List item bulleted", "• Used: 132 GB" in text, text)
check("Table row joined", "Field | Details" in text, text)
check("Empty body → attachment marker", se.clean_comment({"type": "doc", "content": [{"type": "mediaSingle", "content": [{"type": "media"}]}]}) == "[attachment]")

changelog = [
    {"created": "2026-09-21T16:32:22.306+0530", "author": {"displayName": "Automation for Jira"},
     "items": [{"field": "priority", "fromString": "P1", "toString": "P3"}]},
    {"created": "2026-09-21T16:47:47.874+0530", "author": {"displayName": "Parth Salian"},
     "items": [{"field": "assignee", "fromString": None, "toString": "Parth Salian"}]},
    {"created": "2026-09-21T17:08:20.221+0530", "author": {"displayName": "Parth Salian"},
     "items": [{"field": "status", "fromString": "Open", "toString": "Pending"}, {"field": "labels", "fromString": "", "toString": "x"}]},
    {"created": "2026-09-28T14:27:06.291+0530", "author": {"displayName": "Parth Salian"},
     "items": [{"field": "status", "fromString": "Pending", "toString": "Completed"}]},
]
comments = [
    {"created": "2026-09-21T16:58:13.529+0530", "author": {"displayName": "Parth Salian"}, "jsdPublic": True, "body": para(txt("Session link"))},
    {"created": "2026-09-22T09:50:47.874+0530", "author": {"displayName": "Jordan Lee"}, "jsdPublic": False, "body": para(txt("internal note"))},
    {"created": "2026-09-24T10:57:52.095+0530", "author": {"displayName": "Casey <K>"}, "jsdPublic": True, "body": para(txt("Please confirm & share"))},
]
current = {"status": "Completed", "priority": "P3", "assignee": "Parth Salian"}
check("Opening state reconstructed", se.opening_snapshot(current, changelog) ==
      {"status": "Open", "priority": "P1", "assignee": "Unassigned"}, se.opening_snapshot(current, changelog))
tl = se.build_timeline(changelog, comments)
check("Internal notes excluded", all("internal note" not in e.get("text", "") for e in tl))
check("Untracked fields ignored", all(f in se.TRACKED_FIELDS for e in tl if e["kind"] == "change" for f, _, _ in e["changes"]))
check("Chronological order", [e["at"] for e in tl] == sorted(e["at"] for e in tl))
check("6 events (4 changes + 2 public comments)", len(tl) == 6, len(tl))
check("Comment shows the author's role, not their name, and is escaped for Slack",
      "Casey" not in se.format_event(tl[-2]) and ("*Support*" in se.format_event(tl[-2]) or "*Customer*" in se.format_event(tl[-2]))
      and "&amp;" in se.format_event(tl[-2]), se.format_event(tl[-2]))
check("Assignee change shows Unassigned → Assigned, not the person",
      any("Assignee: Unassigned → Assigned" in se.format_event(e) for e in tl)
      and not any("Parth" in se.format_event(e) for e in tl), [se.format_event(e) for e in tl])
check("Field change helper", se.field_change("assignee", "A", "B") == "Assignee: reassigned"
      and se.field_change("assignee", "A", None) == "Assignee: Assigned → Unassigned"
      and se.field_change("status", "Open", "Pending") == "Status: Open → Pending")
check("Change line names no one", se.format_event(tl[0]) == "*21 Sep 16:32* · Priority: P1 → P3", se.format_event(tl[0]))

# N3: Automation's first-seconds changes belong to the opening state (real CASE-4009 pattern).
created = se.parse_jira_time("2026-09-21T16:32:19.229+0530")
auto = [dict(e) for e in changelog]
auto[0] = {**auto[0], "author": {"displayName": "Automation for Jira", "accountType": "app"}}
check("Automation change within 60 s kept in opening state",
      se.opening_snapshot(current, auto, created)["priority"] == "P3", se.opening_snapshot(current, auto, created))
check("Human changes still undone", se.opening_snapshot(current, auto, created)["status"] == "Open")
tl_auto = se.build_timeline(auto, comments, created)
check("Opening Automation change not listed in history", len(tl_auto) == 5 and
      not any("Automation for Jira" in se.format_event(e) for e in tl_auto), len(tl_auto))
late = [dict(e) for e in auto]
late[0] = {**late[0], "created": "2026-09-21T16:40:00.000+0530"}
check("Automation change after the window is history, not opening",
      se.opening_snapshot(current, late, created)["priority"] == "P1" and len(se.build_timeline(late, comments, created)) == 6)
human_fast = [dict(e) for e in changelog]
human_fast[0] = {**human_fast[0], "author": {"displayName": "Parth Salian", "accountType": "atlassian"}}
check("A person's change within the window is still history",
      se.opening_snapshot(current, human_fast, created)["priority"] == "P1")
check("Without a creation time nothing is skipped", se.opening_snapshot(current, auto)["priority"] == "P1")


class FakeSlack:
    def __init__(self):
        self.calls, self.n = [], 0

    def create_channel(self, name):
        self.calls.append(("create", name)); return {"success": True, "channel_id": "C1"}

    def invite_users(self, c, u):
        self.calls.append(("invite", c)); return True

    def send_message(self, c, text, blocks=None):
        self.calls.append(("starter", text)); return True

    def post_message(self, c, text, thread_ts=None):
        self.n += 1; self.calls.append(("post", text, thread_ts)); return f"ts{self.n}"

    def set_topic(self, c, topic):
        self.calls.append(("topic", topic)); return True

    def archive_channel(self, c):
        self.calls.append(("archive", c)); return True


issue = {"key": "CASE-4009", "self": "https://bloo-systems.atlassian.net/rest/api/3/issue/1", "fields": {
    "summary": "ACME|AD servers went offline", "status": {"name": "Completed"}, "priority": {"name": "P2"},
    "assignee": {"displayName": "Parth Salian"}, "issuetype": {"name": "[System] Problem"}, "project": {"key": "CASE"},
    "customfield_10002": [{"name": "ACME"}], "created": "2026-09-21T16:32:19.229+0530"}}
changelog[0]["items"][0]["toString"] = "P3"
changelog.append({"created": "2026-09-24T12:48:16.555+0530", "author": {"displayName": "Parth Salian"},
                  "items": [{"field": "priority", "fromString": "P3", "toString": "P2"}]})

with tempfile.TemporaryDirectory() as tmp:
    os.environ["CTC_LOG_DIR"] = tmp
    slack = FakeSlack()
    engine = se.SyncEngine(slack, se.ChannelState(Path(tmp) / "s.json"), ["Closed"], ["U1"])

    dry = engine.backfill(issue, changelog, comments, dry_run=True)
    check("Dry run makes no Slack calls", dry["outcome"] == "dry_run" and slack.calls == [])

    r = engine.backfill(issue, changelog, comments, pause=0)
    check("Backfill succeeds", r["outcome"] == "backfilled", r.get("outcome"))
    check("Channel named from current priority", slack.calls[0] == ("create", "case-4009-acme-med"), slack.calls[0])
    starter = next(c[1] for c in slack.calls if c[0] == "starter")
    check("Starter shows opening priority", "*Priority check:* P1" in starter, starter)
    posts = [c for c in slack.calls if c[0] == "post"]
    check("History header is top-level", posts[0][2] is None and "Ticket history · CASE-4009" in posts[0][1])
    check("Every event is a thread reply", all(p[2] == "ts1" for p in posts[1:1 + len(r["timeline"])]), posts)
    check("All events posted", r["posted"] == len(r["timeline"]) == 7, (r["posted"], len(r["timeline"])))
    check("Topic set to current state", any(c[0] == "topic" and "Completed" in c[1] and "P2" in c[1] for c in slack.calls))
    check("Current-state summary is last", posts[-1][2] is None and "Synced to current state" in posts[-1][1])
    check("Completed not archived (not in archive list)", not any(c[0] == "archive" for c in slack.calls))
    tracked = se.ChannelState(Path(tmp) / "s.json").tickets["CASE-4009"]
    check("State tracks current values for future syncs", tracked["status"] == "Completed" and tracked["priority"] == "P2")
    rec = [json.loads(l) for l in (Path(tmp) / "tests.jsonl").read_text().splitlines()][-1]
    check("Run log has replay steps", [s["name"] for s in rec["steps"]][-2:] == ["Replay history", "Apply current state"])

    slack.calls.clear()
    again = engine.backfill(issue, changelog, comments, pause=0)
    check("Second backfill refused (already tracked)", again["outcome"] == "already_tracked" and slack.calls == [])

    engine2 = se.SyncEngine(FakeSlack(), se.ChannelState(Path(tmp) / "s2.json"), ["Completed"], [])
    check("Archives when final status is in archive list", engine2.backfill(issue, changelog, comments, pause=0)["archived"] is True)
    os.environ.pop("CTC_LOG_DIR", None)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
