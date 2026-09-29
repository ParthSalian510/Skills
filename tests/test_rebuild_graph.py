#!/usr/bin/env python3
"""Tests for the nightly case-graph rebuild (Graphify is faked). Run: python3 tests/test_rebuild_graph.py"""
import sys
import os
os.environ.setdefault("CTC_RUN_MODE", "test")
import json
import subprocess
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

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


with tempfile.TemporaryDirectory() as tmp:
    os.environ["CTC_INDEX_PATH"] = str(Path(tmp) / "index" / "cases.jsonl")
    os.environ["CTC_LOG_DIR"] = tmp
    import rebuild_graph as rg
    from case_index import CaseIndex

    calls = []
    def fake(returncode=0):
        def run(cmd, **kw):
            calls.append((cmd, kw))
            if cmd[1] == "extract":  # pretend graphify wrote the graph
                out = Path(kw["cwd"]) / "graphify-out"
                out.mkdir(exist_ok=True)
                (out / "graph.json").write_text("{}")
            return subprocess.CompletedProcess(cmd, returncode, stdout="Graph: 3 nodes\ndone", stderr="boom")
        return run

    check("No index yet → nothing to do", rg.main([], runner=fake()) == 0 and calls == [])
    CaseIndex().upsert({"ticket_id": "CASE-1", "title": "t", "components": ["Namenode"], "keywords": []})
    check("New index → rebuild", rg.main([], runner=fake()) == 0 and [c[0][1] for c in calls] == ["extract", "cluster-only"])
    extract, kw = calls[0]
    check("Uses the Claude CLI backend and reads gitignored pages",
          extract[extract.index("--backend") + 1] == "claude-cli" and "--no-gitignore" in extract)
    check("Graphify query log kept off", kw["env"].get("GRAPHIFY_QUERY_LOG_DISABLE") == "1")
    check("Runs inside index/pages", kw["cwd"].endswith(os.path.join("index", "pages")))
    check("Pages exported before extracting", (Path(tmp) / "index" / "pages" / "cases" / "CASE-1.md").exists())

    calls.clear()
    check("Unchanged index → skipped", rg.main([], runner=fake()) == 0 and calls == [])
    check("--force rebuilds anyway, with a full re-extract",
          rg.main(["--force"], runner=fake()) == 0 and "--force" in calls[0][0])

    time.sleep(0.05)
    CaseIndex().upsert({"ticket_id": "CASE-2", "title": "u", "components": [], "keywords": []})
    calls.clear()
    check("Failed extract → exit 1, no clustering", rg.main([], runner=fake(returncode=2)) == 1 and len(calls) == 1)
    logs = [json.loads(l) for l in (Path(tmp) / "tests.jsonl").read_text().splitlines()]
    check("Every run logged", len(logs) >= 5 and logs[-1]["summary"]["final_status"] == "failure", logs[-1]["summary"])
    os.environ.pop("CTC_INDEX_PATH", None)
    os.environ.pop("CTC_LOG_DIR", None)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
