#!/usr/bin/env python3
"""Tests for the Jira poller (fake Jira + fake Slack). Run: python3 tests/test_jira_poller.py"""
import sys
import os
os.environ.setdefault("CTC_RUN_MODE", "test")
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import jira_poller as jp

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


def jira_time(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000%z")


def issue(key, created, status="Pending", priority="P3", assignee="Parth Salian", customer="ACME"):
    return {"key": key, "self": "https://bloo-systems.atlassian.net/rest/api/3/issue/1", "fields": {
        "summary": f"{customer} | test", "status": {"name": status}, "priority": {"name": priority},
        "assignee": {"displayName": assignee}, "issuetype": {"name": "[System] Problem"},
        "project": {"key": key.split("-")[0]}, "customfield_10002": [{"name": customer}],
        "created": jira_time(created), "updated": jira_time(created)}}


class FakeJira:
    def __init__(self):
        self.issues, self.queries, self.fail = [], [], False

    def search(self, jql, fields):
        self.queries.append(jql)
        if self.fail:
            raise RuntimeError("503 Service Unavailable")
        return list(self.issues)


class FakeSlack:
    def __init__(self):
        self.calls, self.existing = [], set()

    def create_channel(self, name):
        self.calls.append(("create", name))
        if name in self.existing:
            return {"success": True, "channel_id": None, "exists": True}
        self.existing.add(name)
        return {"success": True, "channel_id": "C" + name.upper().replace("-", "")[:8]}

    def invite_users(self, channel_id, user_ids):
        self.calls.append(("invite", channel_id))
        return True

    def send_message(self, channel_id, text, blocks=None):
        self.calls.append(("send", channel_id, text))
        return True

    def set_topic(self, channel_id, topic):
        self.calls.append(("topic", channel_id, topic))
        return True

    def archive_channel(self, channel_id):
        self.calls.append(("archive", channel_id))
        return True


def kinds(slack):
    return [c[0] for c in slack.calls]


def read_log(tmp):
    p = Path(tmp) / "tests.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


with tempfile.TemporaryDirectory() as tmp:
    os.environ["CTC_LOG_DIR"] = tmp
    state_path = Path(tmp) / "state" / "channels.json"
    now = datetime.now(timezone.utc)
    jira, slack = FakeJira(), FakeSlack()
    state = jp.ChannelState(state_path)
    check("State file created on first start", state_path.exists())
    poller = jp.Poller(jira, slack, state, ["CASE"], 2, ["Done", "Resolved", "Closed"], ["U1"])

    check("JQL watches configured projects", 'project in (CASE) AND updated >= "-2m"' in poller.jql(), poller.jql())

    # Old ticket (created before the poller started) is ignored; new ticket gets a channel.
    old = issue("CASE-3990", now - timedelta(days=3))
    new = issue("CASE-4020", now + timedelta(seconds=5))
    jira.issues = [old, new]
    counts = poller.poll_once()
    check("Pre-existing ticket ignored", counts.get("ignored") == 1, counts)
    check("New ticket provisioned", counts.get("created") == 1, counts)
    check("Standard channel name", ("create", "case-4020-acme-low") in slack.calls, slack.calls)
    check("Invite + starter message sent", kinds(slack) == ["create", "invite", "send"], kinds(slack))
    tracked = jp.ChannelState(state_path).tickets.get("CASE-4020", {})
    check("Mapping persisted with baseline fields", tracked.get("channel_id") and tracked.get("status") == "Pending")
    check("last_poll_at recorded", jp.ChannelState(state_path).last_poll_at is not None)
    rec = read_log(tmp)[-1]
    check("Provision logged under poller source", rec["source"] == "poller" and rec["summary"]["final_status"] == "success")

    # Same data next poll: nothing happens.
    slack.calls.clear()
    counts = poller.poll_once()
    check("Unchanged ticket does nothing", slack.calls == [] and counts.get("unchanged") == 1, (counts, slack.calls))

    # Priority change: topic + update message, no archive.
    jira.issues = [issue("CASE-4020", now + timedelta(seconds=5), priority="P1")]
    counts = poller.poll_once()
    check("Priority change synced", counts.get("synced") == 1, counts)
    check("Topic then update message", kinds(slack) == ["topic", "send"], kinds(slack))
    check("Topic shows new priority", "P1" in slack.calls[0][2], slack.calls[0])
    check("Update message shows old → new", "Priority: P3 → P1" in slack.calls[1][2], slack.calls[1][2])

    # Assignee-only change: message but no topic change.
    slack.calls.clear()
    jira.issues = [issue("CASE-4020", now + timedelta(seconds=5), priority="P1", assignee="Sam")]
    poller.poll_once()
    check("Assignee change posts note only", kinds(slack) == ["send"], kinds(slack))

    # Resolved: topic, message mentioning archive, archive, then inactive.
    slack.calls.clear()
    jira.issues = [issue("CASE-4020", now + timedelta(seconds=5), status="Resolved", priority="P1", assignee="Sam")]
    poller.poll_once()
    check("Resolve → topic, note, archive", kinds(slack) == ["topic", "send", "archive"], kinds(slack))
    check("Note says channel is being archived", "archiving" in slack.calls[1][2])
    check("Archived flag persisted", jp.ChannelState(state_path).tickets["CASE-4020"]["archived"] is True)
    slack.calls.clear()
    counts = poller.poll_once()
    check("Archived channel no longer synced", slack.calls == [] and counts.get("inactive") == 1, counts)

    # Channel already exists in Slack: remembered, not retried every poll.
    slack.existing.add("case-4021-acme-low")
    slack.calls.clear()
    jira.issues = [issue("CASE-4021", now + timedelta(seconds=5))]
    counts = poller.poll_once()
    check("Existing channel reported, not duplicated", counts.get("existed") == 1 and kinds(slack) == ["create"], counts)
    slack.calls.clear()
    counts = poller.poll_once()
    check("Existing channel not retried", slack.calls == [] and counts.get("inactive") == 1, counts)

    # Manually tracked channel: first poll baselines silently, then syncs changes.
    with poller.state.locked():
        poller.state.tickets["CASE-3997"] = {"channel_id": "C0C4JS4P77E", "channel_name": "case-3997-acme-med", "archived": False}
    slack.calls.clear()
    jira.issues = [issue("CASE-3997", now - timedelta(days=20), priority="P2")]
    counts = poller.poll_once()
    check("Tracked channel baselined without messages", counts.get("baselined") == 1 and slack.calls == [], counts)
    jira.issues = [issue("CASE-3997", now - timedelta(days=20), status="Work in progress", priority="P2")]
    counts = poller.poll_once()
    check("Tracked old ticket synced after baseline", counts.get("synced") == 1, counts)

    # Missing customer: failure logged, no channel, retried next time (not remembered).
    slack.calls.clear()
    jira.issues = [issue("CASE-4022", now + timedelta(seconds=5), customer="")]
    jira.issues[0]["fields"]["customfield_10002"] = []
    counts = poller.poll_once()
    check("Missing customer → no channel", counts.get("failed") == 1 and slack.calls == [], (counts, slack.calls))
    check("Failed ticket not remembered", "CASE-4022" not in poller.state.tickets)

    # Lookback widens after downtime.
    with poller.state.locked():
        poller.state.last_poll_at = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    check("Lookback covers downtime gap", poller.lookback() >= 31, poller.lookback())

    # Jira outage: logged once, no crash, recovery clears the flag.
    jira.fail = True
    before = len(read_log(tmp))
    try:
        poller.poll_once()
        check("Jira error surfaces from poll_once", False)
    except RuntimeError:
        check("Jira error surfaces from poll_once", True)
    check("Failed poll leaves last_poll_at unchanged", poller.lookback() >= 31, poller.lookback())

    # State survives reload.
    reloaded = jp.ChannelState(state_path)
    check("State reloads tracked tickets", set(reloaded.tickets) >= {"CASE-4020", "CASE-4021", "CASE-3997"})
    check("started_at stable across reloads", reloaded.started_at == state.started_at)

    # CLI: track + status against a separate state file.
    cli_state = Path(tmp) / "cli.json"
    jp.main(["--state", str(cli_state), "track", "case-4009", "C0C40SWBJN7", "ticket-case-4009"])
    check("track registers ticket (key upper-cased)", "CASE-4009" in json.loads(cli_state.read_text())["tickets"])

    os.environ.pop("CTC_LOG_DIR", None)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
