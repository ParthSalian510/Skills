#!/usr/bin/env python3
"""Tests for the versioned case-graph rebuild (Graphify is faked). Run: python3 tests/test_rebuild_graph.py"""
import sys
import os
os.environ.setdefault("CTC_RUN_MODE", "test")
import fcntl
import json
import subprocess
import tempfile
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

    calls, alerts = [], []
    notify = lambda text: alerts.append(text) or True
    graph_nodes = {"extra": 0}  # how many extra concept nodes the fake graph has

    def fake(returncode=0, drop_cases=False):
        """Pretend to be graphify: extract writes a graph with one node per indexed ticket."""
        def run(cmd, **kw):
            calls.append((cmd, kw))
            if cmd[1] == "extract" and returncode == 0:
                out = Path(kw["cwd"]) / "graphify-out"
                out.mkdir(exist_ok=True)
                keys = [] if drop_cases else [json.loads(l)["ticket_id"]
                                               for l in Path(os.environ["CTC_INDEX_PATH"]).read_text().splitlines()]
                nodes = [{"id": k, "label": f"{k}: case", "community": 0} for k in keys]
                nodes += [{"id": f"c{i}", "label": f"concept {i}", "community": 1} for i in range(graph_nodes["extra"])]
                links = [{"source": nodes[0]["id"], "target": nodes[-1]["id"], "relation": "references"}] if nodes else []
                (out / "graph.json").write_text(json.dumps({"nodes": nodes, "links": links}))
                (out / "graph.html").write_text("<html></html>")
            return subprocess.CompletedProcess(cmd, returncode, stdout="Graph: done", stderr="boom")
        return run

    graphs = Path(tmp) / "index" / "graphs"
    current = graphs / "current"

    check("No index yet → nothing to do", rg.main([], runner=fake(), notify=notify) == 0 and calls == [])
    for k in ("CASE-1", "CASE-2", "SR-3"):
        CaseIndex().upsert({"ticket_id": k, "title": "t", "components": ["Namenode"], "keywords": []})
    graph_nodes["extra"] = 20
    check("First build publishes a snapshot", rg.main([], runner=fake(), notify=notify) == 0
          and current.is_symlink() and (current / "graph.json").exists(), list(graphs.iterdir()))
    extract, kw = calls[0]
    check("Uses the Claude CLI backend and reads gitignored pages",
          extract[extract.index("--backend") + 1] == "claude-cli" and "--no-gitignore" in extract)
    check("Graphify query log kept off", kw["env"].get("GRAPHIFY_QUERY_LOG_DISABLE") == "1")
    check("Runs inside index/pages", kw["cwd"].endswith(os.path.join("index", "pages")))
    m = json.loads((current / "manifest.json").read_text())
    check("Manifest records counts, coverage and per-case fingerprints",
          m["cases"] == 3 and m["requests"] == 0 and m["coverage"] == 1.0 and set(m["fingerprints"]) == {"CASE-1", "CASE-2", "SR-3"}
          and m["nodes"] == 23, m)
    check("Snapshot carries its own copy of the index", (current / "cases.jsonl").read_text()
          == Path(os.environ["CTC_INDEX_PATH"]).read_text())
    check("current is a relative link (copyable to another host)", not os.readlink(current).startswith("/"))

    calls.clear()
    check("Unchanged index → skipped", rg.main([], runner=fake(), notify=notify) == 0 and calls == [])
    idx = CaseIndex()
    e = idx.get("CASE-1")
    idx.upsert({**e, "indexed_at": "2099-01-01"})
    check("Re-indexed but unchanged case doesn't count", rg.main([], runner=fake(), notify=notify) == 0 and calls == [])
    idx.upsert({**e, "fix": "changed"})
    calls.clear()
    check("--if-changed 10 waits while fewer cases changed", rg.main(["--if-changed", "10"], runner=fake(), notify=notify) == 0
          and calls == [])
    first = os.readlink(current)
    check("Default threshold rebuilds after one change", rg.main([], runner=fake(), notify=notify) == 0
          and os.readlink(current) != first and json.loads((current / "manifest.json").read_text())["changed"] == 1)
    calls.clear()
    check("--force rebuilds anyway, with a full re-extract",
          rg.main(["--force"], runner=fake(), notify=notify) == 0 and "--force" in calls[0][0])

    # failures leave current alone and alert
    before = os.readlink(current)
    idx.upsert({**e, "fix": "changed again"})
    alerts.clear()
    check("Failed extract → exit 1, current untouched, alert sent",
          rg.main([], runner=fake(returncode=2), notify=notify) == 1 and os.readlink(current) == before
          and alerts and "rebuild failed" in alerts[0], alerts)
    graph_nodes["extra"] = 0
    alerts.clear()
    check("Graph that shrank >10% is not published", rg.main([], runner=fake(), notify=notify) == 1
          and os.readlink(current) == before and "nodes fell" in alerts[0], alerts)
    check("--allow-shrink publishes it anyway", rg.main(["--allow-shrink"], runner=fake(), notify=notify) == 0
          and os.readlink(current) != before)
    graph_nodes["extra"] = 20
    idx.upsert({**e, "fix": "changed a third time"})
    alerts.clear()
    before = os.readlink(current)
    check("Graph missing most tickets is not published", rg.main(["--allow-shrink"], runner=fake(drop_cases=True),
                                                                 notify=notify) == 1
          and os.readlink(current) == before and "tickets are in the graph" in alerts[0], alerts)

    # retention and locking
    for i in range(8):
        idx.upsert({**e, "fix": f"v{i}"})
        rg.main([], runner=fake(), notify=notify)
    snaps = [p for p in graphs.iterdir() if p.is_dir() and not p.is_symlink() and p.name[:1].isdigit()]
    check("Keeps the newest 7 snapshots", len(snaps) == 7 and (graphs / os.readlink(current)).exists(), len(snaps))
    with open(graphs / ".build.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        idx.upsert({**e, "fix": "while locked"})
        calls.clear()
        check("A second build while one runs is skipped", rg.main([], runner=fake(), notify=notify) == 0 and calls == [])

    logs = [json.loads(l) for l in (Path(tmp) / "graph.jsonl").read_text().splitlines()] \
        if (Path(tmp) / "graph.jsonl").exists() else \
        [json.loads(l) for l in (Path(tmp) / "tests.jsonl").read_text().splitlines()]
    check("Every run logged", len(logs) >= 10)
    os.environ.pop("CTC_INDEX_PATH", None)
    os.environ.pop("CTC_LOG_DIR", None)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
