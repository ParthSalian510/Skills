#!/usr/bin/env python3
"""Per-source run logs for create-ticket-channel; see SKILL.md "Run Logs" for the record shape."""
import argparse
import json
import os
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
LOG_FILES = {
    "skill": "skill-runs.jsonl",
    "webhook": "webhook.jsonl",
    "poller": "poller.jsonl",
    "executor": "executor.jsonl",
    "graph": "graph.jsonl",
    "test": "tests.jsonl",
}
MODES = ("live", "dry_run", "placeholder", "test")
FINAL_STATUSES = ("success", "failure", "skipped", "dry_run", "rejected")


def log_dir() -> Path:
    return Path(os.environ.get("CTC_LOG_DIR") or SKILL_ROOT / "logs")


def in_test_mode() -> bool:
    return os.environ.get("CTC_RUN_MODE") == "test"


def log_path_for(source: str) -> Path:
    if source not in LOG_FILES:
        raise ValueError(f"Unknown log source {source!r}. Expected one of: {', '.join(LOG_FILES)}")
    return log_dir() / LOG_FILES["test" if in_test_mode() else source]


def resolve_mode(mode: str) -> str:
    return "test" if in_test_mode() else mode


def _parse_steps(raw: str) -> list:
    steps = json.loads(raw)
    if not isinstance(steps, list):
        raise ValueError("--steps must be a JSON list")
    for s in steps:
        missing = {"step", "name", "status"} - set(s)
        if missing:
            raise ValueError(f"step record {s!r} is missing {', '.join(sorted(missing))}")
    return steps


def known_channels(ticket: str) -> list:
    """Channels this skill has already created or found for a ticket, from logs/skill-runs.jsonl.

    Live runs only (never dry runs, tests or placeholders). Used by Step 3 before
    the Slack search, because the search index can lag right after a channel is
    created.
    """
    path = log_dir() / LOG_FILES["skill"]
    if not path.exists():
        return []
    found = []
    for line in path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (rec.get("ticket_id") or "").upper() != ticket.upper() or rec.get("mode") != "live":
            continue
        final = (rec.get("summary") or {}).get("final_status")
        ok = final in ("success", "skipped") or (rec.get("legacy_format") and rec.get("jira_channel_action"))
        if ok and rec.get("channel_name") and rec["channel_name"] not in found:
            found.append(rec["channel_name"])
    return found


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["lookup"]:
        if len(argv) != 2:
            print("usage: run_log.py lookup TICKET", file=sys.stderr)
            return 2
        channels = known_channels(argv[1])
        print(json.dumps({"ticket_id": argv[1].upper(), "known_channels": channels}))
        return 0
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from audit_logger import AuditLogger

    p = argparse.ArgumentParser(description="Append one run record to the create-ticket-channel run logs.")
    p.add_argument("--source", default="skill", choices=sorted(LOG_FILES))
    p.add_argument("--mode", default="live", choices=MODES)
    p.add_argument("--ticket", required=True, help="Ticket key, e.g. CASE-4010")
    p.add_argument("--type", help="Ticket type (project key); defaults to the key prefix")
    p.add_argument("--customer")
    p.add_argument("--priority")
    p.add_argument("--channel", help="Channel created or found, if any")
    p.add_argument("--final", required=True, choices=FINAL_STATUSES)
    p.add_argument("--duration-seconds", type=float)
    p.add_argument("--steps", default="[]",
                   help='JSON list of {"step", "name", "status", optional "duration_seconds", "details", "error"}')
    args = p.parse_args(argv)

    try:
        steps = _parse_steps(args.steps)
    except (ValueError, json.JSONDecodeError) as e:
        p.error(f"invalid --steps: {e}")

    run = AuditLogger(args.ticket, args.type or args.ticket.split("-")[0], args.customer, args.priority,
                      source=args.source, mode=args.mode)
    for s in steps:
        run.record_step(s["step"], s["name"], s["status"], s.get("duration_seconds"),
                        details=s.get("details"), error=s.get("error"))
    entry = run.finalize(args.channel, args.final, duration_seconds=args.duration_seconds)
    print(json.dumps({"run_id": entry["run_id"], "log": str(run.log_path)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
