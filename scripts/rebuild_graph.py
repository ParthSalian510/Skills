#!/usr/bin/env python3
"""Rebuild the case knowledge graph (Graphify) from index/pages/, only when the case index changed.

    python3 scripts/rebuild_graph.py            # nightly timer runs this; skips if nothing changed
    python3 scripts/rebuild_graph.py --force    # full re-extraction (after changing the page format)

Steps: export the Markdown pages from index/cases.jsonl → `graphify extract` (semantic pass through the
Claude Code CLI under this machine's login, no API key) → `graphify cluster-only` (communities, report,
graph.html). Output: index/pages/graphify-out/. Everything stays under index/, which is gitignored.
Graphify's query log is kept off. Each run writes one record to logs/graph.jsonl.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_logger import AuditLogger  # noqa: E402
from case_index import CaseIndex, export_pages  # noqa: E402


def graphify_bin() -> str:
    return os.environ.get("GRAPHIFY_BIN") or shutil.which("graphify") or str(Path.home() / ".local" / "bin" / "graphify")


def needs_rebuild(index_path: Path, graph_path: Path) -> bool:
    return index_path.exists() and (not graph_path.exists() or index_path.stat().st_mtime > graph_path.stat().st_mtime)


def main(argv=None, runner=subprocess.run) -> int:
    p = argparse.ArgumentParser(description="Rebuild the Graphify case graph if the case index changed.")
    p.add_argument("--force", action="store_true", help="rebuild even if nothing changed; re-extract every page")
    args = p.parse_args(argv)

    index = CaseIndex()
    pages = index.path.parent / "pages"
    graph = pages / "graphify-out" / "graph.json"
    run = AuditLogger("case-graph", None, None, None, source="graph")
    if not args.force and not needs_rebuild(index.path, graph):
        print("Case graph is up to date.")
        run.record_step(1, "Check for changes", "skipped", 0, details={"reason": "index unchanged"})
        run.finalize(None, "skipped")
        return 0

    t = time.time()
    counts = export_pages(index, pages)
    run.record_step(1, "Export pages", "success", time.time() - t, details=counts)

    env = {**os.environ, "GRAPHIFY_QUERY_LOG_DISABLE": "1"}
    steps = [("Extract graph", [graphify_bin(), "extract", ".", "--backend", "claude-cli", "--no-gitignore"]
              + (["--force"] if args.force else [])),
             ("Cluster and report", [graphify_bin(), "cluster-only", ".", "--backend=claude-cli"])]
    for n, (name, cmd) in enumerate(steps, start=2):
        t = time.time()
        try:
            proc = runner(cmd, cwd=str(pages), env=env, capture_output=True, text=True, timeout=1800)
        except (OSError, subprocess.TimeoutExpired) as e:
            run.record_step(n, name, "failure", time.time() - t, error={"type": type(e).__name__, "message": str(e)[:300]})
            run.finalize(None, "failure")
            print(f"{name} failed: {e}", file=sys.stderr)
            return 1
        tail = (proc.stdout or "").strip().splitlines()[-3:]
        ok = proc.returncode == 0
        run.record_step(n, name, "success" if ok else "failure", time.time() - t,
                        details={"output": tail}, error=None if ok else {"type": "exit", "message": (proc.stderr or "")[-300:]})
        if not ok:
            run.finalize(None, "failure")
            print(f"{name} failed (exit {proc.returncode}):\n{proc.stderr[-800:]}", file=sys.stderr)
            return 1
        print("\n".join(tail))
    run.finalize(None, "success")
    return 0


if __name__ == "__main__":
    sys.exit(main())
