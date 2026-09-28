#!/usr/bin/env python3
"""Tests for per-source run logs. Run: python3 tests/test_run_log.py"""
import sys
import os
os.environ.setdefault("CTC_RUN_MODE", "test")
import json
import subprocess
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_log
from audit_logger import AuditLogger

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


def lines(path):
    return [json.loads(l) for l in Path(path).read_text().splitlines()]


with tempfile.TemporaryDirectory() as tmp:
    env = dict(os.environ, CTC_LOG_DIR=tmp)

    os.environ["CTC_LOG_DIR"] = tmp
    os.environ["CTC_RUN_MODE"] = "live"
    check("skill → skill-runs.jsonl", run_log.log_path_for("skill") == Path(tmp) / "skill-runs.jsonl")
    check("webhook → webhook.jsonl", run_log.log_path_for("webhook") == Path(tmp) / "webhook.jsonl")
    check("executor → executor.jsonl", run_log.log_path_for("executor") == Path(tmp) / "executor.jsonl")
    try:
        run_log.log_path_for("nope")
        check("Unknown source rejected", False)
    except ValueError:
        check("Unknown source rejected", True)

    os.environ["CTC_RUN_MODE"] = "test"
    check("Test mode routes every source to tests.jsonl",
          {run_log.log_path_for(s).name for s in run_log.LOG_FILES} == {"tests.jsonl"})
    check("Test mode overrides the record's mode", run_log.resolve_mode("live") == "test")

    log = AuditLogger("CASE-1", "CASE", "ACME", "P3", source="webhook", mode="live")
    log.record_step(1, "Generate channel name", "success", None)
    entry = log.finalize("case-1-acme-low", "success")
    check("AuditLogger defaults to per-source path", log.log_path == Path(tmp) / "tests.jsonl")
    check("Record has source/mode", entry["source"] == "webhook" and entry["mode"] == "test")
    check("Unknown duration is omitted, not zero", "duration_seconds" not in entry["steps"][0])

    live_env = dict(env, CTC_RUN_MODE="live")
    steps = json.dumps([{"step": 0, "name": "Pre-flight checks", "status": "success", "duration_seconds": 2.1},
                        {"step": 4, "name": "Browser automation", "status": "success"}])
    out = subprocess.run([sys.executable, str(SCRIPTS / "run_log.py"), "--source", "skill", "--mode", "live",
                          "--ticket", "CASE-4010", "--customer", "ACME", "--priority", "P3",
                          "--channel", "case-4010-acme-low", "--final", "success", "--duration-seconds", "41.5",
                          "--steps", steps], env=live_env, capture_output=True, text=True)
    check("CLI exits 0", out.returncode == 0, out.stderr)
    rec = lines(Path(tmp) / "skill-runs.jsonl")[-1]
    check("CLI writes a skill record", rec["source"] == "skill" and rec["mode"] == "live")
    check("CLI infers ticket type", rec["ticket_type"] == "CASE")
    check("CLI keeps given duration", rec["duration_seconds"] == 41.5)
    check("CLI records steps", [s["step"] for s in rec["steps"]] == [0, 4])
    check("CLI prints run_id", json.loads(out.stdout)["run_id"] == rec["run_id"])

    bad = subprocess.run([sys.executable, str(SCRIPTS / "run_log.py"), "--ticket", "X-1", "--final", "success",
                          "--steps", '[{"step": 1}]'], env=live_env, capture_output=True, text=True)
    check("CLI rejects incomplete step records", bad.returncode != 0 and "missing" in bad.stderr)

    os.environ.pop("CTC_LOG_DIR", None)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
