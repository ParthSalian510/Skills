#!/usr/bin/env python3
"""Rebuild the case knowledge graph (Graphify) as a checked, versioned snapshot.

    python3 scripts/rebuild_graph.py                    # nightly: rebuild if any case changed since the last snapshot
    python3 scripts/rebuild_graph.py --if-changed 10    # hourly: rebuild only once 10+ cases changed
    python3 scripts/rebuild_graph.py --force            # full re-extraction (after changing the page format)

Steps: export the Markdown pages from index/cases.jsonl → `graphify extract` (semantic pass through the Claude
Code CLI under this machine's login, no API key) → `graphify cluster-only` → check the result → copy it into
index/graphs/<UTC time>/ with its own copy of the index and a manifest → atomically point index/graphs/current
at it → keep the newest snapshots. Readers (the case-graph MCP server, `case_index.py connect`) use
index/graphs/current/graph.json; Graphify's server notices the swap and reloads by itself.

A build that fails or fails its checks changes nothing: `current` keeps serving the last good graph and a
Slack alert goes out. One build at a time (a lock file). Everything stays under index/, which is gitignored.
Graphify's query log is kept off. Each run writes one record to logs/graph.jsonl.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_logger import AuditLogger  # noqa: E402
from case_index import CaseIndex, export_pages  # noqa: E402

KEEP_SNAPSHOTS = 7
MAX_SHRINK = 0.10       # a graph that loses more than 10% of its nodes is suspect
MIN_COVERAGE = 0.90     # at least 90% of indexed tickets must appear as nodes
SNAPSHOT_FILES = ("graph.json", "graph.html", "GRAPH_REPORT.md")


def graphify_bin() -> str:
    return os.environ.get("GRAPHIFY_BIN") or shutil.which("graphify") or str(Path.home() / ".local" / "bin" / "graphify")


def fingerprints(index: CaseIndex) -> Dict[str, str]:
    """ticket → hash of its entry (minus bookkeeping), so a re-indexed but unchanged case doesn't count."""
    out = {}
    for e in index.entries():
        body = {k: v for k, v in e.items() if k not in ("indexed_at",)}
        out[e["ticket_id"]] = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    return out


def changed_since(current: Dict[str, str], previous: Dict[str, str]) -> List[str]:
    return sorted(k for k, h in current.items() if previous.get(k) != h)


def read_manifest(graphs: Path) -> Optional[Dict[str, Any]]:
    m = graphs / "current" / "manifest.json"
    return json.loads(m.read_text()) if m.exists() else None


def check_graph(graph_path: Path, index: CaseIndex, previous: Optional[Dict[str, Any]],
                allow_shrink: bool = False) -> Dict[str, Any]:
    """Counts plus a list of problems; an empty list means the graph may be published."""
    problems = []
    try:
        g = json.loads(graph_path.read_text())
    except (OSError, ValueError) as e:
        return {"problems": [f"graph.json unreadable: {e}"]}
    nodes, links = g.get("nodes") or [], g.get("links") or g.get("edges") or []
    keys = set()
    for n in nodes:
        m = re.match(r"([A-Z]+-\d+)", str(n.get("label") or ""))
        if m:
            keys.add(m.group(1))
    tickets = [e["ticket_id"] for e in index.entries()]
    found = sum(1 for t in tickets if t in keys)
    coverage = found / len(tickets) if tickets else 1.0
    communities = len({n.get("community") for n in nodes if n.get("community") is not None})
    if not nodes or not links:
        problems.append("graph is empty")
    if previous and not allow_shrink and len(nodes) < (1 - MAX_SHRINK) * previous.get("nodes", 0):
        problems.append(f"nodes fell from {previous['nodes']} to {len(nodes)} (more than {MAX_SHRINK:.0%})")
    if coverage < MIN_COVERAGE:
        problems.append(f"only {found} of {len(tickets)} tickets are in the graph ({coverage:.0%})")
    return {"nodes": len(nodes), "links": len(links), "communities": communities,
            "coverage": round(coverage, 3), "problems": problems}


def publish(graphs: Path, out_dir: Path, index: CaseIndex, manifest: Dict[str, Any], keep: int) -> Path:
    """Copy the build into a new snapshot folder, then swap `current` to it in one step and prune old ones."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    snap = graphs / stamp
    snap.mkdir(parents=True)
    for name in SNAPSHOT_FILES:
        if (out_dir / name).exists():
            shutil.copy2(out_dir / name, snap / name)
    shutil.copy2(index.path, snap / "cases.jsonl")  # the index this graph was built from, for consistent lookups
    (snap / "manifest.json").write_text(json.dumps({**manifest, "snapshot": stamp}, indent=2))
    tmp = graphs / ".current.tmp"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(stamp)                 # relative link, so the folder can be copied to another host intact
    os.replace(tmp, graphs / "current")   # atomic: readers see the old graph or the new one, never a mix
    snaps = sorted(p for p in graphs.iterdir() if p.is_dir() and not p.is_symlink() and p.name[:1].isdigit())
    for old in snaps[:-keep]:
        shutil.rmtree(old)
    return snap


def slack_notify(text: str) -> bool:
    """Alert the invite list (DM, or #case-index with @-mentions) using the poller's Slack bot."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return False
    try:
        from webhook_server import SlackMessenger
        from sync_engine import ChannelState
    except Exception:
        return False
    slack = SlackMessenger(token)
    users = [u.strip() for u in os.environ.get("SLACK_INVITE_USER_IDS", "").split(",") if u.strip()]
    if any([slack.post_message(u, text) for u in users]):
        return True
    state = ChannelState(Path(__file__).resolve().parent.parent / "state" / "channels.json")
    cid = state.meta.get("index_channel_id")
    mentions = " ".join(f"<@{u}>" for u in users)
    return bool(cid and slack.post_message(cid, f"{mentions} {text}".strip()))


def fail(run: AuditLogger, step: int, name: str, seconds: float, message: str, notify) -> int:
    run.record_step(step, name, "failure", seconds, error={"type": "build", "message": message[:500]})
    run.finalize(None, "failure")
    print(f"{name} failed: {message}", file=sys.stderr)
    notify(f":warning: *Case graph rebuild failed* at \"{name}\": {message[:200]}\n"
           f"The previous graph is still being served. Logs: `journalctl --user -u ctc-graph-rebuild`.")
    return 1


def main(argv=None, runner=subprocess.run, notify: Callable[[str], bool] = slack_notify) -> int:
    p = argparse.ArgumentParser(description="Rebuild the Graphify case graph as a checked, versioned snapshot.")
    p.add_argument("--force", action="store_true", help="rebuild even if nothing changed; re-extract every page")
    p.add_argument("--if-changed", type=int, default=1, metavar="N",
                   help="only rebuild once at least N cases changed since the last snapshot (default 1)")
    p.add_argument("--keep", type=int, default=KEEP_SNAPSHOTS, help=f"snapshots to keep (default {KEEP_SNAPSHOTS})")
    p.add_argument("--allow-shrink", action="store_true", help="publish even if the graph lost >10%% of its nodes")
    args = p.parse_args(argv)

    index = CaseIndex()
    pages = index.path.parent / "pages"
    graphs = index.path.parent / "graphs"
    run = AuditLogger("case-graph", None, None, None, source="graph")
    if not index.path.exists():
        print("No case index yet.")
        run.record_step(1, "Check for changes", "skipped", 0, details={"reason": "no index"})
        run.finalize(None, "skipped")
        return 0
    graphs.mkdir(parents=True, exist_ok=True)

    with open(graphs / ".build.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another graph build is running; skipping.")
            run.finalize(None, "skipped")
            return 0

        previous = read_manifest(graphs)
        prints = fingerprints(index)
        changed = changed_since(prints, (previous or {}).get("fingerprints", {}))
        removed = sorted(set((previous or {}).get("fingerprints", {})) - set(prints))
        threshold = max(1, args.if_changed)
        if not args.force and previous and len(changed) + len(removed) < threshold:
            print(f"Case graph is up to date ({len(changed) + len(removed)} changed, threshold {threshold}).")
            run.record_step(1, "Check for changes", "skipped", 0,
                            details={"changed": len(changed) + len(removed), "threshold": threshold})
            run.finalize(None, "skipped")
            return 0

        t = time.time()
        counts = export_pages(index, pages)
        run.record_step(1, "Export pages", "success", time.time() - t,
                        details={**counts, "changed": len(changed), "removed": len(removed)})

        env = {**os.environ, "GRAPHIFY_QUERY_LOG_DISABLE": "1"}
        steps = [("Extract graph", [graphify_bin(), "extract", ".", "--backend", "claude-cli", "--no-gitignore"]
                  + (["--force"] if args.force else [])),
                 ("Cluster and report", [graphify_bin(), "cluster-only", ".", "--backend=claude-cli"])]
        for n, (name, cmd) in enumerate(steps, start=2):
            t = time.time()
            try:
                proc = runner(cmd, cwd=str(pages), env=env, capture_output=True, text=True, timeout=3600)
            except (OSError, subprocess.TimeoutExpired) as e:
                return fail(run, n, name, time.time() - t, f"{type(e).__name__}: {e}", notify)
            tail = (proc.stdout or "").strip().splitlines()[-3:]
            if proc.returncode != 0:
                return fail(run, n, name, time.time() - t,
                            f"exit {proc.returncode}: {(proc.stderr or '')[-300:]}", notify)
            run.record_step(n, name, "success", time.time() - t, details={"output": tail})
            print("\n".join(tail))

        result = check_graph(pages / "graphify-out" / "graph.json", index, previous, args.allow_shrink)
        if result["problems"]:
            return fail(run, 4, "Check graph", 0, "; ".join(result["problems"]), notify)
        run.record_step(4, "Check graph", "success", 0, details={k: v for k, v in result.items() if k != "problems"})

        manifest = {"built_at": datetime.now(timezone.utc).isoformat(), "cases": counts["cases"],
                    "requests": counts["requests"], "nodes": result["nodes"], "links": result["links"],
                    "communities": result["communities"], "coverage": result["coverage"],
                    "changed": len(changed), "removed": len(removed),
                    "index_sha256": hashlib.sha256(index.path.read_bytes()).hexdigest(), "fingerprints": prints}
        snap = publish(graphs, pages / "graphify-out", index, manifest, args.keep)
        run.record_step(5, "Publish snapshot", "success", 0, details={"snapshot": snap.name})
        run.finalize(None, "success")
        print(f"Published {snap.name}: {result['nodes']} nodes, {result['links']} links, "
              f"{result['communities']} topics, {result['coverage']:.0%} of tickets covered.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
