#!/usr/bin/env python3
"""Tests for the case index: resolution summaries, index/cases.jsonl, #case-index, and closing a ticket.
Run: python3 tests/test_case_index.py (no network; the CLI, Jira and Slack are faked)."""
import sys
import os
os.environ.setdefault("CTC_RUN_MODE", "test")
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import case_index as ci
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


def entry(key, **kw):
    return {"ticket_id": key, "title": kw.get("title", "t"), "customer": kw.get("customer", "ACME"),
            "problem": kw.get("problem", ""), "root_cause": kw.get("root_cause", ""), "fix": kw.get("fix", ""),
            "components": kw.get("components", []), "keywords": kw.get("keywords", []),
            "closed": kw.get("closed", "2026-09-28")}

# ---- the index file
with tempfile.TemporaryDirectory() as tmp:
    idx = ci.CaseIndex(Path(tmp) / "index" / "cases.jsonl")
    check("Empty index", idx.entries() == [] and idx.search("anything") == [])
    check("First upsert adds", idx.upsert(entry("case-4009", problem="Adapters offline", components=["Namenode"],
                                                keywords=["oom killer"])) is False)
    idx.upsert(entry("CASE-3997", problem="High memory on Datanode", keywords=["eventstore"], closed="2026-09-20"))
    check("Re-index replaces, never duplicates",
          idx.upsert(entry("CASE-4009", problem="Adapters offline", components=["Namenode", "CORE"], keywords=["oom killer"])) is True
          and len(idx.entries()) == 2)
    check("Keys stored upper-case", idx.get("case-4009")["ticket_id"] == "CASE-4009")
    hits = idx.search("namenode oom")
    check("Search ranks by matching fields", hits and hits[0]["ticket_id"] == "CASE-4009", [h["ticket_id"] for h in hits])
    check("Search by ticket key", idx.search("CASE-3997")[0]["ticket_id"] == "CASE-3997")
    check("No match → nothing", idx.search("printer jam") == [])
    (Path(tmp) / "index" / "cases.jsonl").open("a").write("not json\n")
    check("Corrupt line skipped", len(idx.entries()) == 2)
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "case_index.py"), "--index", str(idx.path),
                          "search", "oom"], capture_output=True, text=True)
    check("CLI search prints the case", "CASE-4009" in out.stdout and "Problem:" in out.stdout, out.stdout)
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "case_index.py"), "--index", str(idx.path),
                          "show", "CASE-1"], capture_output=True, text=True)
    check("CLI show of unknown ticket → exit 1", out.returncode == 1)

# ---- related cases and Markdown export
with tempfile.TemporaryDirectory() as tmp:
    idx = ci.CaseIndex(Path(tmp) / "cases.jsonl")
    idx.upsert(entry("CASE-1", title="Adapters offline", customer="ACME", components=["Namenode service", "CORE server"],
                     keywords=["oom killer"], closed="2026-09-28"))
    idx.upsert(entry("CASE-2", title="Datanode memory", customer="Beta", components=["NameNode"], keywords=["oom killer"],
                     closed="2026-09-20"))
    idx.upsert(entry("CASE-3", title="ACME | Login broken", customer="ACME", keywords=["sso"],
                     problem="ACME users could not sign in; Beta was fine", fix="Reset the acme SSO realm",
                     components=["Console", "ACME test setup"]))
    check("Concept keys normalise names", ci.concept("Namenode service") == ci.concept("NameNode") == "namenode"
          and ci.concept("Adapter queues") == "adapter queue" and ci.concept("Falcon Sensor (CrowdStrike)") == "falcon sensor"
          and ci.concept("High EPS") == "high eps" and ci.concept("Slow queries") == "slow query")
    rel = ci.related(idx, "CASE-1")
    check("Related finds cases sharing components/keywords", [e["ticket_id"] for e in rel] == ["CASE-2"]
          and rel[0]["shared"] == ["namenode", "oom killer"], rel)
    check("Unrelated case not listed", all(e["ticket_id"] != "CASE-3" for e in rel))
    check("Unknown ticket → no related", ci.related(idx, "CASE-9") == [])
    out = Path(tmp) / "pages"
    counts = ci.export_pages(idx, out)
    check("Export writes a page per case", sorted(p.name for p in (out / "cases").iterdir()) == ["CASE-1.md", "CASE-2.md", "CASE-3.md"])
    page = (out / "cases" / "CASE-1.md").read_text()
    check("Case page links concepts with wikilinks", "[[namenode|Namenode service]]" in page, page)
    check("Customer left off the page entirely", "ACME" not in page and "customer" not in page.lower(), page)
    p3 = (out / "cases" / "CASE-3.md").read_text()
    check("Customer name stripped from title and summary text",
          "acme" not in p3.lower() and "# CASE-3 · Login broken" in p3
          and "the customer users could not sign in" in p3 and "Reset the customer SSO realm" in p3
          and "the customer test setup" in p3, p3)
    check("Other customers named on a page become 'another customer'", "Beta" not in p3 and "another customer was fine" in p3, p3)
    check("Customer still in the index itself", idx.get("CASE-3")["customer"] == "ACME")
    check("Product version is plain text, not a graph link", "[[version" not in page)
    check("Concepts in only one case stay plain text", "CORE server" in page and "[[core" not in page, page)
    nn = (out / "concepts" / "namenode.md").read_text()
    check("Concept page lists every case that involves it", "[[CASE-1]]" in nn and "[[CASE-2]]" in nn, nn)
    (out / "graphify-out").mkdir()
    (out / "graphify-out" / "graph.json").write_text("{}")
    ci.export_pages(idx, out)
    check("Re-export keeps graph tool output next to the pages", (out / "graphify-out" / "graph.json").exists())
    check("Re-export replaces, never accumulates", len(list((out / "cases").iterdir())) == 3 and counts["cases"] == 3)

check("index/ is gitignored", "index/" in (ROOT / ".gitignore").read_text())

# ---- resolution summary parsing
def fake_cli(result):
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(
            {"type": "result", "subtype": "success", "is_error": False, "result": result}), stderr="")
    return run

people = [{"id": "1", "body": "x", "author": {"displayName": "Alex Rivera", "accountType": "atlassian"}}]
with tempfile.TemporaryDirectory() as tmp:
    good = sm.ClaudeCLISummarizer(workdir=Path(tmp), runner=fake_cli(
        '```json\n{"problem": "Adapters offline; Alex restarted CORE. Call +91 9000012345", "root_cause": "OOM killer",'
        ' "fix": "Reboot", "components": ["CORE", "Namenode"], "keywords": ["oom", "adapters offline"]}\n```'))
    r = good.resolution({"ticket_id": "CASE-1"}, people, str)
    check("Resolution JSON parsed through a code fence", r and r["root_cause"] == "OOM killer" and r["components"] == ["CORE", "Namenode"], r)
    check("Names and phone numbers scrubbed from fields", "Alex" not in r["problem"] and "9000012345" not in r["problem"], r["problem"])
    bad = sm.ClaudeCLISummarizer(workdir=Path(tmp), runner=fake_cli("Sorry, I can't."))
    check("Non-JSON reply → None", bad.resolution({"ticket_id": "CASE-1"}, people, str) is None)
    empty = sm.ClaudeCLISummarizer(workdir=Path(tmp), runner=fake_cli('{"problem": "", "fix": "x"}'))
    check("No problem statement → None", empty.resolution({"ticket_id": "CASE-1"}, people, str) is None)

# ---- engine: closing a ticket
class FakeSlack:
    def __init__(self):
        self.calls = []
    def create_channel(self, name):
        self.calls.append(("create", name))
        return {"success": True, "channel_id": "CIDX"}
    def invite_users(self, cid, users):
        self.calls.append(("invite", cid))
        return True
    def set_topic(self, cid, topic):
        self.calls.append(("topic", cid))
        return True
    def post_message(self, cid, text, thread_ts=None):
        self.calls.append(("post", cid, text))
        return "1.0"
    def send_message(self, cid, text, blocks=None):
        self.calls.append(("send", cid, text))
        return True
    def archive_channel(self, cid):
        self.calls.append(("archive", cid))
        return True

class FakeSummarizer:
    def __init__(self, res):
        self.res = res
    def resolution(self, ticket, comments, text_of):
        return self.res
    def update(self, *a):
        return ""

RES = {"problem": "Adapters offline", "root_cause": "OOM <killer>", "fix": "Reboot & restart",
       "components": ["CORE"], "keywords": ["oom"]}

def issue(status="Pending"):
    return {"key": "CASE-4009", "self": "https://api.atlassian.com/ex/jira/x/rest/api/3/issue/1", "fields": {
        "summary": "ACME|AD servers went offline", "status": {"name": status}, "priority": {"name": "P2"},
        "assignee": {"displayName": "Parth Salian"}, "project": {"key": "CASE"}, "customfield_10002": [{"name": "ACME"}],
        "customfield_10171": {"value": "9.2.0"}, "created": "2026-09-21T16:32:19.229+0530",
        "resolutiondate": "2026-09-28T14:27:06.211+0530"}}

with tempfile.TemporaryDirectory() as tmp:
    os.environ["CTC_LOG_DIR"] = tmp
    state = se.ChannelState(Path(tmp) / "channels.json")
    idx = ci.CaseIndex(Path(tmp) / "cases.jsonl")
    slack = FakeSlack()
    eng = se.SyncEngine(slack, state, ["Completed"], invite_user_ids=["U1"], summarizer=FakeSummarizer(RES),
                        fetch_comments=lambda k: [{"id": "1", "jsdPublic": True, "body": "b"},
                                                  {"id": "2", "jsdPublic": False, "body": "internal"}],
                        summary_tickets=[], case_index=idx)
    with state.locked():
        state.tickets["CASE-4009"] = {"channel_id": "C4009", "channel_name": "case-4009-acme-med", "archived": False,
                                      "status": "Pending", "priority": "P2", "assignee": "Parth Salian"}
    check("Indexing on for every ticket by default", eng.indexing_for("CASE-4009"))

    out = eng.process(issue("Completed"), "poller")
    kinds = [c[0] + ":" + c[1] for c in slack.calls]
    check("Closing a ticket syncs it", out == "synced", out)
    check("#case-index created once, people invited, topic set",
          [k for k in kinds if k.endswith(("case-index", "CIDX"))][:3] == ["create:case-index", "invite:CIDX", "topic:CIDX"]
          and kinds.count("create:case-index") == 1, kinds)
    posts = [c for c in slack.calls if c[0] == "post"]
    check("Posted to #case-index and to the ticket channel", {p[1] for p in posts} == {"CIDX", "C4009"}, posts)
    idx_msg = next(p[2] for p in posts if p[1] == "CIDX")
    check("Index message format", idx_msg.startswith("*CASE-4009* · ACME · 9.2.0 · P2 — ACME|AD servers went offline\n*Problem:* Adapters offline")
          and "<#C4009>" in idx_msg and "https://bloo-systems.atlassian.net/browse/CASE-4009" in idx_msg, idx_msg)
    check("Model text escaped for Slack", "OOM &lt;killer&gt;" in idx_msg and "Reboot &amp; restart" in idx_msg)
    check("Ticket channel gets a resolution summary", next(p[2] for p in posts if p[1] == "C4009").startswith("*Resolution summary · CASE-4009*"))
    order = [c[0] + ":" + c[1] for c in slack.calls if c[1] == "C4009"]
    check("Resolution posted before the channel is archived", order.index("post:C4009") < order.index("archive:C4009"), order)
    e = idx.get("CASE-4009")
    check("Entry saved with Jira dates and counts only public comments",
          e and e["opened"].startswith("2026-09-21") and e["closed"].startswith("2026-09-28") and e["public_comments"] == 1, e)
    check("Entry links the Slack channel", e["slack_channel_name"] == "case-4009-acme-med")
    check("Index channel id kept in state", se.ChannelState(Path(tmp) / "channels.json").meta.get("index_channel_id") == "CIDX")
    check("Ticket marked indexed", se.ChannelState(Path(tmp) / "channels.json").tickets["CASE-4009"].get("indexed") is True)

    # CLI-style indexing of an untracked closed ticket reuses the channel
    slack.calls.clear()
    with state.locked():
        r = eng.close_case({**issue("Completed"), "key": "CASE-3001"}, [], tracked=None)
    check("Untracked closed ticket indexed without creating a new channel",
          r["outcome"] == "indexed" and not any(c[0] == "create" for c in slack.calls)
          and [c[1] for c in slack.calls if c[0] == "post"] == ["CIDX"], slack.calls)

    slack.calls.clear()
    r = eng.close_case({**issue("Completed"), "key": "CASE-3002"}, [], tracked=None, post=False)
    check("Index-only (post=False) writes the entry but posts nothing",
          r["outcome"] == "indexed" and idx.get("CASE-3002") and slack.calls == [], slack.calls)
    dry = eng.close_case(issue("Completed"), [], dry_run=True)
    check("Dry run writes and posts nothing", dry["outcome"] == "dry_run" and "*Problem:*" in dry["message"])

    # failed summary: nothing written or posted, channel still archived
    fail_eng = se.SyncEngine(slack, state, ["Completed"], summarizer=FakeSummarizer(None),
                             fetch_comments=lambda k: [], case_index=idx)
    with state.locked():
        state.tickets["CASE-5000"] = {"channel_id": "C5000", "channel_name": "case-5000-acme-low", "archived": False,
                                      "status": "Pending", "priority": "P2", "assignee": "Parth Salian"}
    slack.calls.clear()
    before = len(idx.entries())
    out = fail_eng.process({**issue("Completed"), "key": "CASE-5000"}, "poller")
    check("Failed resolution summary: nothing indexed or posted", len(idx.entries()) == before
          and not any(c[0] == "post" for c in slack.calls), slack.calls)
    check("…but the channel is still closed out", ("archive", "C5000") in slack.calls)

    off = se.SyncEngine(slack, state, ["Completed"], case_index=idx)
    check("No summarizer → no indexing", off.indexing_for("CASE-4009") is False)
    os.environ.pop("CTC_LOG_DIR", None)

# ---- similar past cases for a new ticket
with tempfile.TemporaryDirectory() as tmp:
    idx = ci.CaseIndex(Path(tmp) / "cases.jsonl")
    idx.upsert(entry("CASE-10", title="High memory utilization on DN", components=["Datanode"], keywords=["high memory utilization"],
                     fix="Reduced concurrent workbooks.", closed="2026-09-10"))
    idx.upsert(entry("CASE-11", title="Console login fails", components=["Console"], keywords=["sso"]))
    hits = ci.similar(idx, "ACME | High memory utilization\nNotable events on the datanode keep firing")
    check("Shortlist finds the matching case", [h["ticket_id"] for h in hits] == ["CASE-10"], hits)
    check("Stopwords and weak overlaps don't match", ci.similar(idx, "Please check the issue with the team") == [])
    check("A ticket never matches itself", ci.similar(idx, "High memory utilization datanode", exclude="case-10") == [])

    def picker(result):
        def run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(
                {"type": "result", "subtype": "success", "is_error": False, "result": result}), stderr="")
        return sm.ClaudeCLISummarizer(workdir=Path(tmp), runner=run)
    cand = ci.similar(idx, "High memory utilization datanode")
    picks = picker('[{"ticket_id": "CASE-10", "why": "same DN memory symptom"}, {"ticket_id": "CASE-999", "why": "made up"}]').pick_related(
        {"summary": "High memory"}, cand)
    check("Claude's picks limited to the shortlist", picks == [{"ticket_id": "CASE-10", "why": "same DN memory symptom"}], picks)
    check("Empty pick list allowed", picker("[]").pick_related({"summary": "x"}, cand) == [])
    check("Garbage reply → None", picker("no idea").pick_related({"summary": "x"}, cand) in (None, []))

    class PickSummarizer(FakeSummarizer):
        def __init__(self, picks):
            super().__init__(RES)
            self.picks = picks
        def pick_related(self, ticket, candidates):
            return self.picks
    slack = FakeSlack()
    eng = se.SyncEngine(slack, se.ChannelState(Path(tmp) / "s.json"), [], summarizer=PickSummarizer(
        [{"ticket_id": "CASE-10", "why": "same DN memory symptom"}]), case_index=idx)
    t = {"ticket_id": "CASE-20", "summary": "ACME | High memory utilization", "description": "datanode alerts", "customer": "ACME"}
    check("Related cases posted", eng.post_similar_cases(t, "CNEW") == "posted")
    msg = slack.calls[-1][2]
    check("Note lists case, reason and fix", "CASE-10" in msg and "same DN memory symptom" in msg and "Reduced concurrent workbooks." in msg, msg)
    check("Note names no customer", "ACME" not in msg, msg)
    none = se.SyncEngine(slack, se.ChannelState(Path(tmp) / "s2.json"), [], summarizer=PickSummarizer([]), case_index=idx)
    before = len(slack.calls)
    check("No good match → nothing posted", none.post_similar_cases(t, "CNEW") == "none" and len(slack.calls) == before)
    off = se.SyncEngine(slack, se.ChannelState(Path(tmp) / "s3.json"), [], summarizer=PickSummarizer([]), case_index=idx,
                        similar_cases=False)
    check("similar_on_new can be switched off", off.similar_on_new is False)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
