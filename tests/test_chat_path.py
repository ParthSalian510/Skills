#!/usr/bin/env python3
"""End-to-end check of the chat skill's scripted steps, from a real Atlassian MCP response.

The MCP calls and the browser step can only run inside Claude, but everything
between them is deterministic. This runs that part on tests/fixtures/mcp_case_4009.json
(a real getJiraIssue result): Step 1 fields → Step 2 name → Step 3 run-log
lookup → Step 7 starter message. It also checks SKILL.md and config still
point at these scripts.
Run: python3 tests/test_chat_path.py
"""
import sys
import os
os.environ.setdefault("CTC_RUN_MODE", "test")
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import yaml
import run_log
import starter_message
import webhook_server as ws
from generate_channel_name import generate_channel_name, load_config
from ticket_sync import TicketSyncManager

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


FIXTURE = ROOT / "tests" / "fixtures" / "mcp_case_4009.json"
SKILL = (ROOT / "SKILL.md").read_text()
CONFIG = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())

# Step 1: the MCP response shape the skill actually receives.
issue = starter_message.find_issue(json.loads(FIXTURE.read_text()))
check("Issue found inside MCP {issues: {nodes: [...]}}", issue and issue["key"] == "CASE-4009")
fields = issue["fields"]
for f in ("customfield_10002", "customfield_10171", "customfield_10194", "description", "priority", "project"):
    check(f"SKILL.md Step 1 requests {f}", f'"{f}"' in SKILL.split("### Step 1")[1].split("### Step 2")[0])

# Step 2: name from the same fields.
name, errors = generate_channel_name(issue["key"], fields["project"]["key"], fields["customfield_10002"][0]["name"],
                                     fields["priority"]["name"], load_config(str(ROOT / "config" / "config.yaml")))
check("Step 2 name follows the standard", name == "case-4009-isoc-med" and not errors, (name, errors))

# Step 3: run-log lookup finds live runs only.
with tempfile.TemporaryDirectory() as tmp:
    os.environ["CTC_LOG_DIR"] = tmp
    check("Lookup with no log → nothing known", run_log.known_channels("CASE-4009") == [])
    records = [
        {"ticket_id": "CASE-4009", "mode": "live", "channel_name": "case-4009-isoc-med",
         "summary": {"final_status": "success"}},
        {"ticket_id": "CASE-4010", "mode": "dry_run", "channel_name": "case-4010-isoc-low",
         "summary": {"final_status": "dry_run"}},
        {"ticket_id": "CASE-4011", "mode": "live", "channel_name": "case-4011-isoc-low",
         "summary": {"final_status": "failure"}},
        {"ticket_id": "SR-4058", "mode": "live", "legacy_format": "flat-v1", "channel_name": "sr-4058-aail-low",
         "jira_channel_action": "created"},
    ]
    (Path(tmp) / "skill-runs.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
    check("Live success found (case-insensitive key)", run_log.known_channels("case-4009") == ["case-4009-isoc-med"])
    check("Dry runs don't count", run_log.known_channels("CASE-4010") == [])
    check("Failed runs don't count", run_log.known_channels("CASE-4011") == [])
    check("Legacy flat-v1 records count", run_log.known_channels("SR-4058") == ["sr-4058-aail-low"])
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "run_log.py"), "lookup", "CASE-4009"],
                         capture_output=True, text=True, env={**os.environ, "CTC_LOG_DIR": tmp})
    check("lookup CLI prints JSON", json.loads(out.stdout)["known_channels"] == ["case-4009-isoc-med"], out.stdout)
    os.environ.pop("CTC_LOG_DIR", None)

# Step 7: the exact starter message.
EXPECTED = """*Organisation:* ISOC
*Title:* ISOC|AD servers went offline
*Priority check:* P2
*Type Check:* [System] Problem (CASE)
*Product Version Check:* 9.2.0
*Description:* Most of the Adapters went to offline. Kindly check. Status: Completed.

*Jira:* <https://bloo-systems.atlassian.net/browse/CASE-4009|CASE-4009>"""
out = subprocess.run([sys.executable, str(ROOT / "scripts" / "starter_message.py"), str(FIXTURE)],
                     capture_output=True, text=True)
check("Starter message matches the team template exactly", out.stdout.strip() == EXPECTED, out.stdout)
check("Greeting stripped from description", "Hello Team" not in out.stdout)
check("Jira link uses the site, not the API gateway", "api.atlassian.com" not in out.stdout)
bad = subprocess.run([sys.executable, str(ROOT / "scripts" / "starter_message.py")], input='{"foo": 1}',
                     capture_output=True, text=True)
check("No issue in input → exit 1", bad.returncode == 1 and "No Jira issue" in bad.stderr, bad.stderr)
no_version = json.loads(FIXTURE.read_text())
del no_version["issues"]["nodes"][0]["fields"]["customfield_10171"]
ticket = ws.WebhookValidator.extract_event_data({"webhookEvent": "jira:issue_created", "issue": starter_message.find_issue(no_version)})
check("Product Version falls back to customfield_10194", ticket["product_version"] == "V9", ticket["product_version"])

# Link base for every way an issue arrives.
check("Gateway self URL → site", ws.jira_browse_base(
    {"self": "https://api.atlassian.com/ex/jira/x/rest/api/3/issue/1"}) == "https://bloo-systems.atlassian.net")
check("Site self URL kept", ws.jira_browse_base(
    {"self": "https://bloo-systems.atlassian.net/rest/api/2/issue/1"}) == "https://bloo-systems.atlassian.net")

# Config and SKILL.md wiring.
check("Starter message on by default", CONFIG["skill"]["starter_message"] is True)
check("SKILL.md Step 7 uses the script", "scripts/starter_message.py" in SKILL.split("### Step 7")[1].split("### Step 8")[0])
check("SKILL.md Step 3 checks the run log first", "run_log.py lookup" in SKILL.split("### Step 3")[1].split("### Step 4")[0])

# G2: the manager that makes no Slack calls must not claim it did.
result = TicketSyncManager({"archive_on_status": ["Completed"]}).sync_ticket(
    "CASE-4009", "case-4009-isoc-med", {"status": "Completed", "priority": "P2"}, "https://x")
statuses = {a["status"] for a in result.get("actions_taken", [])}
check("TicketSyncManager actions are 'planned', never 'completed'", statuses == {"planned"}, statuses)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
