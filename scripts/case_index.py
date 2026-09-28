#!/usr/bin/env python3
"""The case index: one entry per closed ticket (problem, root cause, fix), for looking up past cases.

Entries live in index/cases.jsonl (gitignored: it holds customer case details) and are
also posted to the #case-index Slack channel by the sync engine. Re-indexing a ticket
replaces its entry, so the file has at most one line per ticket.

    python3 scripts/case_index.py search "namenode oom"      # best matches first
    python3 scripts/case_index.py show CASE-4009
    python3 scripts/case_index.py list
"""
import argparse
import fcntl
import json
import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INDEX_PATH = SKILL_ROOT / "index" / "cases.jsonl"
SEARCH_FIELDS = {"title": 3, "problem": 3, "root_cause": 3, "fix": 2, "components": 3, "keywords": 3,
                 "customer": 2, "product_version": 1, "ticket_id": 5}


def default_index_path() -> Path:
    return Path(os.environ.get("CTC_INDEX_PATH") or DEFAULT_INDEX_PATH)


class CaseIndex:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path or default_index_path())

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def entries(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def get(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        return next((e for e in self.entries() if e.get("ticket_id") == ticket_id.upper()), None)

    def upsert(self, entry: Dict[str, Any]) -> bool:
        """Add or replace the entry for entry["ticket_id"]. Returns True if it replaced one."""
        key = entry["ticket_id"].upper()
        with self._locked():
            rest = [e for e in self.entries() if e.get("ticket_id") != key]
            replaced = len(rest) != len(self.entries())
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in rest + [{**entry, "ticket_id": key}]))
            tmp.replace(self.path)
        return replaced

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Rank entries by how many query words appear in their fields (weighted); no match → not returned."""
        words = [w for w in re.findall(r"[\w.-]+", query.lower()) if len(w) > 1]
        scored = []
        for e in self.entries():
            score = 0
            for field, weight in SEARCH_FIELDS.items():
                value = e.get(field)
                text = " ".join(value) if isinstance(value, list) else str(value or "")
                text = text.lower()
                score += weight * sum(1 for w in words if w in text)
            if score:
                scored.append((score, e.get("closed") or "", e))
        scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
        return [e for _, _, e in scored[:limit]]


def format_entry(e: Dict[str, Any]) -> str:
    """Plain-text view for the CLI."""
    lines = [f"{e['ticket_id']} · {e.get('customer') or '?'} · {e.get('product_version') or 'no version'} · "
             f"{e.get('priority') or '?'} · closed {(e.get('closed') or '?')[:10]}",
             f"  {e.get('title') or ''}",
             f"  Problem:    {e.get('problem') or '-'}",
             f"  Root cause: {e.get('root_cause') or '-'}",
             f"  Fix:        {e.get('fix') or '-'}"]
    if e.get("components"):
        lines.append(f"  Components: {', '.join(e['components'])}")
    if e.get("jira_url"):
        lines.append(f"  {e['jira_url']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Search the case index of closed tickets.")
    p.add_argument("--index", default=str(default_index_path()))
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=5)
    sh = sub.add_parser("show")
    sh.add_argument("ticket_id")
    sub.add_parser("list")
    args = p.parse_args(argv)
    index = CaseIndex(Path(args.index))
    if args.cmd == "search":
        hits = index.search(args.query, args.limit)
        print("\n\n".join(format_entry(e) for e in hits) if hits else "No matching cases.")
    elif args.cmd == "show":
        e = index.get(args.ticket_id)
        print(format_entry(e) if e else f"{args.ticket_id.upper()} is not in the index.")
        return 0 if e else 1
    else:
        for e in index.entries():
            print(f"{e['ticket_id']:<11} {(e.get('closed') or '')[:10]}  {e.get('customer') or '?':<14} {e.get('title') or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
