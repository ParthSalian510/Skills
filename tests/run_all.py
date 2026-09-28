#!/usr/bin/env python3
"""Run every tests/test_*.py script and log one record per script to logs/tests.jsonl."""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / "scripts"))
os.environ["CTC_RUN_MODE"] = "test"

from audit_logger import AuditLogger


def main() -> int:
    run = AuditLogger("test-suite", None, None, None, source="test", mode="test")
    failures = 0
    for i, script in enumerate(sorted(TESTS.glob("test_*.py"))):
        start = time.time()
        proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=300)
        m = re.search(r"(\d+) passed, (\d+) failed", proc.stdout)
        counts = {"passed": int(m[1]), "failed": int(m[2])} if m else {}
        ok = proc.returncode == 0
        failures += not ok
        run.record_step(i, script.name, "success" if ok else "failure", time.time() - start,
                        details=counts or None,
                        error=None if ok else {"type": "test_failure", "message": proc.stdout[-800:] + proc.stderr[-800:]})
        print(f"{'PASS' if ok else 'FAIL'}  {script.name}  {counts or ''}")
    run.finalize(None, "success" if not failures else "failure")
    print(f"\n{len(run.steps) - failures}/{len(run.steps)} test scripts passed · logged to {run.log_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
