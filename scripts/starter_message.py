#!/usr/bin/env python3
"""Print the team's starter message for a ticket, from Jira issue JSON.

The chat skill (SKILL.md Step 7) saves the Atlassian MCP getJiraIssue result to
a file and runs this, so the message is built by the same code the poller uses
instead of being written by the model:

    python3 scripts/starter_message.py /tmp/case-4009.json

Accepts the MCP response ({"issues": {"nodes": [issue]}}), a bare issue
({"key", "fields"}), or a webhook event ({"issue": ...}). Reads stdin if no
path is given. Exit 1 with a message on stderr if the input has no issue.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from webhook_server import WebhookValidator, format_starter_message  # noqa: E402


def find_issue(data):
    if isinstance(data, dict):
        nodes = (data.get("issues") or {}).get("nodes") if isinstance(data.get("issues"), dict) else None
        if nodes:
            return nodes[0]
        if isinstance(data.get("issues"), list) and data["issues"]:
            return data["issues"][0]
        if isinstance(data.get("issue"), dict):
            return data["issue"]
        if data.get("key") and isinstance(data.get("fields"), dict):
            return data
    return None


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    raw = Path(argv[0]).read_text() if argv else sys.stdin.read()
    issue = find_issue(json.loads(raw))
    if not issue:
        print("No Jira issue found in the input (expected getJiraIssue output).", file=sys.stderr)
        return 1
    ticket = WebhookValidator.extract_event_data({"webhookEvent": "jira:issue_created", "issue": issue})
    if not ticket:
        print("Could not read the issue's fields.", file=sys.stderr)
        return 1
    print(format_starter_message(ticket)["text"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
