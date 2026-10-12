#!/usr/bin/env python3
"""Tests for change → incident links (SR changes that may have triggered later CASE incidents).
Run: python3 tests/test_change_links.py (no network; Claude and Slack are faked)."""
import sys
import os
os.environ.setdefault("CTC_RUN_MODE", "test")
import json
import subprocess
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import case_index as ci
import change_links as cl
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


def case(key, opened, customer="ACME", **kw):
    return {"ticket_id": key, "title": kw.get("title", "t"), "customer": customer, "opened": opened,
            "closed": kw.get("closed", opened), "problem": kw.get("problem", ""), "root_cause": kw.get("root_cause", ""),
            "fix": "f", "components": kw.get("components", []), "keywords": kw.get("keywords", [])}


def change(key, opened, closed, customer="ACME", category="extractor or parser (incl. parsing issues)", **kw):
    return {"ticket_id": key, "kind": "request", "title": kw.get("title", "Apply extractor"), "customer": customer,
            "opened": opened, "closed": closed, "request": "r", "category": category,
            "work_done": kw.get("work_done", "Applied the new firewall extractor."), "outcome": kw.get("outcome", "completed"),
            "components": kw.get("components", []), "keywords": kw.get("keywords", []),
            "jira_url": f"https://example.atlassian.net/browse/{key}"}


class FakeSlack:
    def __init__(self):
        self.calls = []

    def post_message(self, channel, text):
        self.calls.append(("post", channel, text))
        return True


with tempfile.TemporaryDirectory() as tmp:
    idx = ci.CaseIndex(Path(tmp) / "cases.jsonl")
    # filler so "generic" has meaning: a concept on most tickets
    for i in range(80):
        idx.upsert(case(f"CASE-{900 + i}", "2026-01-01", customer="Other", components=["Pico"]))
    idx.upsert(change("SR-1", "2026-09-01", "2026-09-05", components=["Pico", "Firewall extractor"]))
    idx.upsert(case("CASE-10", "2026-09-10", title="Firewall logs unparsed", components=["Pico", "Firewall extractor"]))
    idx.upsert(case("CASE-11", "2026-09-12", title="Console slow", problem="Console slow after the parser was renamed"))
    idx.upsert(case("CASE-12", "2026-09-12", title="Console slow", components=["Pico"]))       # only a generic concept
    idx.upsert(case("CASE-13", "2026-11-20", components=["Firewall extractor"]))                # too late
    idx.upsert(case("CASE-14", "2026-08-01", components=["Firewall extractor"]))                # before the change
    idx.upsert(case("CASE-15", "2026-09-10", customer="Beta", components=["Firewall extractor"]))  # other customer
    idx.upsert(change("SR-2", "2026-09-01", "2026-09-05", outcome="no response from customer",
                      components=["Firewall extractor"]))                                       # nothing was changed
    idx.upsert(change("SR-3", "2026-09-01", "2026-09-05", category="access or user management",
                      components=["Firewall extractor"]))                                       # not a change kind

    pairs = cl.candidates(idx)
    got = {(p["change"], p["incident"]) for p in pairs}
    check("Shortlist: same customer, in the window, specific concept or change cue",
          got == {("SR-1", "CASE-10"), ("SR-1", "CASE-11")}, sorted(got))
    p10 = next(p for p in pairs if p["incident"] == "CASE-10")
    check("Generic concepts don't count, specific ones do", "pico" not in p10["shared"] and p10["shared"], p10)
    check("Cue alone (case text names the kind of change) is enough",
          next(p for p in pairs if p["incident"] == "CASE-11")["cue"])
    check("Best pair first", pairs[0]["incident"] == "CASE-10")

    calls = []

    def judge(sr, cases):
        calls.append((sr["ticket_id"], [c["ticket_id"] for c in cases]))
        return {"CASE-10": {"verdict": "likely", "why": "logs went unparsed after ACME's extractor at 10.1.2.3"},
                "CASE-11": {"verdict": "nonsense", "why": "x"}}
    store = cl.LinkStore.beside(idx)
    r = cl.link(idx, judge, store)
    check("One Claude call per change, with all its cases", calls == [("SR-1", ["CASE-10", "CASE-11"])], calls)
    check("Unknown verdict → unrelated", store.pairs["SR-1>CASE-11"]["verdict"] == "unrelated")
    check("Verdicts saved beside the index", json.loads((Path(tmp) / "change_links.json").read_text())["pairs"]["SR-1>CASE-10"]
          ["verdict"] == "likely" and r["pending"] == 0, r)
    calls.clear()
    check("Judged pairs are not asked again", cl.link(idx, judge)["calls"] == 0 and calls == [])
    e = idx.get("CASE-11")
    idx.upsert({**e, "indexed_at": "2099"})
    check("Re-indexing without a change doesn't re-ask", cl.link(idx, judge)["calls"] == 0)
    idx.upsert({**e, "root_cause": "a new root cause"})
    cl.link(idx, judge)
    check("A changed ticket is asked about again", calls == [("SR-1", ["CASE-11"])], calls)
    calls.clear()
    r = cl.link(idx, lambda sr, cases: None)
    check("A failed call leaves the pairs as they were", r["failed"] == 0 and r["calls"] == 0)
    idx.upsert({**idx.get("CASE-10"), "fix": "changed"})
    r = cl.link(idx, lambda sr, cases: None)
    check("Failed call → counted, pair stays pending", r["failed"] == 1 and r["pending"] == 1, r)
    cl.link(idx, judge)
    check("max_calls limits Claude calls", cl.link(idx, judge, max_calls=0)["calls"] == 0)

    shown = cl.links(idx)
    check("links(): likely and possible only", [(l["change"], l["incident"], l["verdict"]) for l in shown]
          == [("SR-1", "CASE-10", "likely")], shown)
    bt = cl.by_ticket(idx)
    check("by_ticket(): both ends", set(bt) == {"SR-1", "CASE-10"})

    # pages: explicit wikilinks both ways, no customer or host in the reason
    pages = Path(tmp) / "pages"
    ci.export_pages(idx, pages)
    sr_page = (pages / "requests" / "SR-1.md").read_text()
    case_page = (pages / "cases" / "CASE-10.md").read_text()
    check("Change page links forward", "**Followed by:** [[CASE-10]] (likely:" in sr_page, sr_page)
    check("Incident page links back", "**Possibly caused by:** [[SR-1]] (likely:" in case_page, case_page)
    check("Reason has no customer name or IP", "ACME" not in case_page and "10.1.2.3" not in case_page, case_page)
    check("Unrelated pairs leave no link", "caused by" not in (pages / "cases" / "CASE-11.md").read_text())

    # pairs that drop off the shortlist are forgotten
    idx.upsert({**idx.get("CASE-10"), "customer": "Beta"})
    r = cl.link(idx, judge)
    check("Pair no longer shortlisted → dropped", "SR-1>CASE-10" not in cl.LinkStore.beside(idx).pairs and r["dropped"] == 1, r)
    idx.upsert({**idx.get("CASE-10"), "customer": "ACME"})

    # recent changes for a new ticket
    today = date(2026, 9, 20)
    rc = cl.recent_changes(idx, "acme", "Firewall logs not parsing", today=today)
    check("Recent changes: this customer's done changes in the last 30 days",
          [c["ticket_id"] for c in rc] == ["SR-1"] and rc[0]["days_ago"] == 15, rc)
    check("Older than the window → none", cl.recent_changes(idx, "ACME", "", today=date(2026, 12, 1)) == [])
    check("Other customer / no customer → none", cl.recent_changes(idx, "Beta", "", today=today) == []
          and cl.recent_changes(idx, "", "", today=today) == [])

    # the new-ticket note in Slack
    class Picks:
        def __init__(self, picks):
            self.picks = picks
        def pick_related(self, ticket, candidates):
            return self.picks
    orig = cl.recent_changes
    se_rc = lambda index, customer, text: orig(index, customer, text, today=today)
    import change_links
    change_links.recent_changes = se_rc
    slack = FakeSlack()
    eng = se.SyncEngine(slack, se.ChannelState(Path(tmp) / "s.json"), [], summarizer=Picks([]), case_index=idx)
    t = {"ticket_id": "CASE-50", "summary": "Firewall logs not parsing", "description": "", "customer": "ACME"}
    check("No related cases but a recent change → note posted", eng.post_similar_cases(t, "CNEW") == "posted")
    msg = slack.calls[-1][2]
    check("Note lists the change, its type and when it closed",
          "Recent changes for this customer" in msg and "SR-1" in msg and "extractor or parser ·" in msg
          and "15 days ago" in msg and "Possibly related past cases" not in msg, msg)
    eng2 = se.SyncEngine(slack, se.ChannelState(Path(tmp) / "s2.json"), [], summarizer=Picks(None), case_index=idx)
    check("Claude failed but a change exists → still posted", eng2.post_similar_cases(t, "CNEW") == "posted")
    t2 = {**t, "customer": "Nobody", "summary": "zzz qqq"}
    check("No match and no change → nothing", eng.post_similar_cases(t2, "CNEW") == "none")
    change_links.recent_changes = orig

    # Claude's reply
    def claude(result):
        def run(cmd, **kw):
            run.prompt = kw["input"]
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(
                {"type": "result", "subtype": "success", "is_error": False, "result": result}), stderr="")
        return sm.ClaudeCLISummarizer(workdir=Path(tmp), runner=run), run
    s, run = claude('[{"ticket_id": "CASE-10", "verdict": "Likely", "why": "parsing broke after it"},'
                    ' {"ticket_id": "CASE-999", "verdict": "likely", "why": "made up"}]')
    v = s.judge_changes(idx.get("SR-1"), [idx.get("CASE-10"), idx.get("CASE-11")])
    check("judge_changes: only listed cases, verdict lower-cased",
          v == {"CASE-10": {"verdict": "likely", "why": "parsing broke after it"}}, v)
    check("Prompt carries the change and the cases with dates", "SR-1 (closed 2026-09-05)" in run.prompt
          and "CASE-11 (opened 2026-09-12)" in run.prompt and "Work done:" in run.prompt, run.prompt[:400])
    check("Garbage reply → None", claude("no idea")[0].judge_changes(idx.get("SR-1"), [idx.get("CASE-10")]) is None)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
