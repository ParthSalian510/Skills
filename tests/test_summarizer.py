#!/usr/bin/env python3
"""Tests for comment summaries: scrubbing, the claude CLI call, and the sync engine's comment cursor.
Run: python3 tests/test_summarizer.py (no network; the CLI and Slack are faked)."""
import sys
import os
os.environ.setdefault("CTC_RUN_MODE", "test")
import json
import subprocess
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import summarizer as sm
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


# ---- scrubbing and names
raw = "Call +91 9000012345 or agent@example.com\nhttps://zoom.us/j/1?pwd=x\nMeeting ID: 986 0842 9853\nPasscode: 813965\n=====\nCORE at 160 GB, v9.2.0"
clean = sm.scrub(raw)
check("Phone removed", "9000012345" not in clean and "[phone]" in clean, clean)
check("E-mail removed", "agent@example.com" not in clean)
check("Meeting ID and passcode lines removed", "813965" not in clean and "986 0842" not in clean, clean)
check("URLs replaced", "zoom.us" not in clean and "[link]" in clean)
check("Technical numbers kept", "160 GB" in clean and "9.2.0" in clean, clean)
check("Role from account type", sm.role_of({"author": {"accountType": "customer"}}) == "Customer"
      and sm.role_of({"author": {"accountType": "atlassian"}}) == "Support")
people = [{"author": {"displayName": "Alex Rivera", "accountType": "atlassian"}},
          {"author": {"displayName": "acme support", "accountType": "customer"}}]
names = sm._names(people)
out = sm.ClaudeCLISummarizer()._clean("Alex restarted it; acme support confirmed; Rivera checked the support queue.", names)
check("Names replaced by role in one pass (no chaining)",
      out == "support restarted it; the customer confirmed; support checked the support queue.", out)

# ---- the CLI call
calls = []
def runner_returning(payload, raise_exc=None):
    def run(cmd, **kw):
        calls.append((cmd, kw))
        if raise_exc:
            raise raise_exc
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")
    return run

with tempfile.TemporaryDirectory() as tmp:
    ok = sm.ClaudeCLISummarizer(claude_path="/bin/claude", workdir=Path(tmp),
                                runner=runner_returning({"type": "result", "subtype": "success", "is_error": False,
                                                         "result": "*Problem:* Adapters offline. Call +91 9000012345."}))
    ticket = {"ticket_id": "CASE-1", "summary": "ACME | adapters", "status": "Pending", "priority": "P2"}
    comments = [{"id": "10", "created": "2026-09-21T10:00:00.000+0530", "body": "Hello, adapters are offline. Regards, Alex",
                 "author": {"displayName": "Alex Rivera", "accountType": "atlassian"}}]
    result = ok.case_summary(ticket, comments, str)
    cmd, kw = calls[-1]
    check("Case summary returned and scrubbed", result == "*Problem:* Adapters offline. Call [phone].", result)
    check("All tools disabled", cmd[cmd.index("--tools") + 1] == "")
    check("MCP, settings and skills disabled", "--strict-mcp-config" in cmd and cmd[cmd.index("--setting-sources") + 1] == ""
          and "--disable-slash-commands" in cmd and "--no-session-persistence" in cmd)
    check("Runs in its own empty directory", kw["cwd"] == tmp)
    check("Has a timeout", kw["timeout"] == 180)
    check("Prompt has roles, not names", "[Support · 2026-09-21]" in kw["input"] and "Alex Rivera" not in kw["input"].split("Comments")[1].split("]")[0])
    check("Comment text wrapped as data", "<ticket>" in kw["input"] and "</ticket>" in kw["input"])

    noop = sm.ClaudeCLISummarizer(workdir=Path(tmp), runner=runner_returning(
        {"type": "result", "subtype": "success", "is_error": False, "result": "NO_UPDATE"}))
    check("NO_UPDATE → empty string", noop.update(ticket, comments, [], str) == "")
    err = sm.ClaudeCLISummarizer(workdir=Path(tmp), runner=runner_returning({"type": "result", "subtype": "error", "is_error": True}))
    check("CLI error → None", err.case_summary(ticket, comments, str) is None)
    slow = sm.ClaudeCLISummarizer(workdir=Path(tmp), runner=runner_returning({}, subprocess.TimeoutExpired("claude", 180)))
    check("Timeout → None", slow.case_summary(ticket, comments, str) is None)
    check("Config off → no summarizer", sm.from_config({"summaries": {"enabled": False}}) is None)
    check("Config on → model from config", sm.from_config({"summaries": {"enabled": True, "model": "m"}}).model == "m")


# ---- engine: comment cursor and posts
class FakeSlack:
    def __init__(self):
        self.posts = []
    def post_message(self, channel_id, text, thread_ts=None):
        self.posts.append((channel_id, text, thread_ts))
        return f"{len(self.posts)}.0"
    def send_message(self, channel_id, text, blocks=None):
        self.posts.append((channel_id, text, None))
        return True
    def set_topic(self, *a):
        return True
    def archive_channel(self, *a):
        return True

class FakeSummarizer:
    def __init__(self, update_result="• Namenode killed by the OOM killer.", case_result="*Problem:* x"):
        self.update_result, self.case_result, self.calls = update_result, case_result, []
    def update(self, ticket, new, earlier, text_of):
        self.calls.append(("update", [c["id"] for c in new], [c["id"] for c in earlier]))
        return self.update_result
    def case_summary(self, ticket, comments, text_of):
        self.calls.append(("case", [c["id"] for c in comments]))
        return self.case_result

def issue(status="Pending"):
    return {"key": "CASE-3997", "self": "https://api.atlassian.com/ex/jira/x/rest/api/3/issue/1", "fields": {
        "summary": "ACME | memory", "status": {"name": status}, "priority": {"name": "P2"},
        "assignee": {"displayName": "Parth Salian"}, "issuetype": {"name": "[System] Problem"},
        "project": {"key": "CASE"}, "customfield_10002": [{"name": "ACME"}], "created": "2026-09-03T15:01:58.950+0530"}}

def c(id_, public=True, text="note"):
    return {"id": str(id_), "created": "2026-09-28T10:00:00.000+0530", "jsdPublic": public, "body": text,
            "author": {"displayName": "Someone", "accountType": "atlassian"}}

with tempfile.TemporaryDirectory() as tmp:
    os.environ["CTC_LOG_DIR"] = tmp
    state = se.ChannelState(Path(tmp) / "channels.json")
    with state.locked():
        state.tickets["CASE-3997"] = {"channel_id": "C1", "channel_name": "case-3997-acme-med", "archived": False}
    jira_comments = [c(100), c(101)]
    slack, fake = FakeSlack(), FakeSummarizer()
    eng = se.SyncEngine(slack, state, ["Completed"], summarizer=fake, fetch_comments=lambda k: list(jira_comments),
                        summary_tickets=["CASE-3997"])

    check("Baseline poll", eng.process(issue(), "poller") == "baselined")
    check("First sight records cursor, posts nothing", state.tickets["CASE-3997"]["last_comment_id"] == 101 and slack.posts == [])
    check("No new comments → unchanged", eng.process(issue(), "poller") == "unchanged" and slack.posts == [])

    jira_comments += [c(102), c(103, public=False, text="internal"), c(104)]
    check("New public comments summarised", eng.process(issue(), "poller") == "summarized")
    check("Only new public comments sent, earlier ones as context",
          fake.calls[-1] == ("update", ["102", "104"], ["100", "101"]), fake.calls[-1])
    text = slack.posts[-1][1]
    check("Update note format", text.startswith("*Update · CASE-3997* · 2 new public comments\n• Namenode") and
          "https://bloo-systems.atlassian.net/browse/CASE-3997" in text, text)
    check("Cursor advanced past internal note", state.tickets["CASE-3997"]["last_comment_id"] == 104)
    check("Same comments not summarised twice", eng.process(issue(), "poller") == "unchanged")

    fake.update_result = ""
    jira_comments.append(c(105, text="Please join the call"))
    check("Nothing of substance → skipped, no post", eng.process(issue(), "poller") == "comments_skipped" and len(slack.posts) == 1)

    fake.update_result = None
    jira_comments.append(c(106, text="SECRET raw text"))
    posts_before = len(slack.posts)
    check("Summary failure → nothing posted, retried next poll", eng.process(issue(), "poller") == "summary_retry"
          and len(slack.posts) == posts_before)
    check("Cursor stays put while retrying", state.tickets["CASE-3997"]["last_comment_id"] == 105
          and "summary_failing_since" in state.tickets["CASE-3997"])
    fake.update_result = "• Recovered summary"
    check("Retry succeeds once the summarizer is back", eng.process(issue(), "poller") == "summarized"
          and "Recovered summary" in slack.posts[-1][1])
    check("Success moves the cursor and clears the failure clock", state.tickets["CASE-3997"]["last_comment_id"] == 106
          and "summary_failing_since" not in state.tickets["CASE-3997"])
    fake.update_result = None
    jira_comments.append(c(1061, text="SECRET raw text"))
    eng.process(issue(), "poller")
    with state.locked():
        state.tickets["CASE-3997"]["summary_failing_since"] -= se.SUMMARY_RETRY_SECONDS + 1
    check("Still failing after the retry window → fallback note", eng.process(issue(), "poller") == "failed")
    check("Fallback never copies comment text", "SECRET" not in slack.posts[-1][1] and "Summary unavailable" in slack.posts[-1][1])
    check("Cursor moves on after the fallback", state.tickets["CASE-3997"]["last_comment_id"] == 1061
          and "summary_failing_since" not in state.tickets["CASE-3997"])
    jira_comments.pop()  # back to comments 101–106 for the tests below
    with state.locked():
        state.tickets["CASE-3997"]["last_comment_id"] = 106

    fake.update_result = "• Status note"
    jira_comments.append(c(107))
    check("Field change and comments in one poll", eng.process(issue("Under investigation"), "poller") == "synced"
          and any("Update · CASE-3997" in p[1] for p in slack.posts[-2:]))

    other = se.SyncEngine(slack, state, [], summarizer=fake, fetch_comments=lambda k: 1 / 0, summary_tickets=["CASE-1"])
    check("Tickets not opted in never fetch comments", other.summaries_for("CASE-3997") is False)
    check("'*' opts in every ticket", se.SyncEngine(slack, state, [], summarizer=fake, fetch_comments=list,
                                                    summary_tickets=["*"]).summaries_for("CASE-9"))

    # summary so far
    so_far = eng.summarize_so_far(issue(), jira_comments, dry_run=True)
    check("Summary-so-far dry run posts nothing", so_far["outcome"] == "dry_run" and so_far["message"].startswith(
        "*Case summary so far · CASE-3997* · from 7 public comments (1 internal notes not included)"), so_far["message"][:120])
    with state.locked():
        state.tickets["CASE-3997"]["last_comment_id"] = 100
    before = len(slack.posts)
    posted = eng.summarize_so_far(issue(), jira_comments)
    check("Summary so far posted", posted["outcome"] == "posted" and len(slack.posts) == before + 1)
    check("Summary so far moves the cursor to the newest comment", state.tickets["CASE-3997"]["last_comment_id"] == 107)
    fake.case_result = None
    check("Failed summary so far posts nothing", eng.summarize_so_far(issue(), jira_comments)["outcome"] == "failed"
          and len(slack.posts) == before + 1)

    # backfill with summaries: summary as parent, only field changes in the thread
    fake.case_result = "*Problem:* Memory\n*Status:* Pending."
    changelog = [{"created": "2026-09-04T10:00:00.000+0530", "author": {"displayName": "P", "accountType": "atlassian"},
                  "items": [{"field": "status", "fromString": "Open", "toString": "Pending"}]}]
    bf = se.SyncEngine(slack, se.ChannelState(Path(tmp) / "bf.json"), [], summarizer=fake)
    plan = bf.backfill(issue(), changelog, [c(1), c(2, public=False), c(3)], dry_run=True)
    check("Backfill header is the case summary", plan["header"].startswith("*Case summary · CASE-3997* · from 2 public comments")
          and "*Problem:* Memory" in plan["header"], plan["header"])
    check("Backfill thread has only field changes", plan["timeline"] == ["*04 Sep 10:00* · Status: Open → Pending"], plan["timeline"])
    os.environ.pop("CTC_LOG_DIR", None)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
