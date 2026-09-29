#!/usr/bin/env python3
"""The case index: one entry per closed ticket (problem, root cause, fix), for looking up past cases.

Entries live in index/cases.jsonl (gitignored: it holds customer case details) and are
also posted to the #case-index Slack channel by the sync engine. Re-indexing a ticket
replaces its entry, so the file has at most one line per ticket.

    python3 scripts/case_index.py search "namenode oom"      # best matches first
    python3 scripts/case_index.py show CASE-4009
    python3 scripts/case_index.py list
    python3 scripts/case_index.py related CASE-4009     # past cases sharing components / keywords
    python3 scripts/case_index.py export                # Markdown pages with [[wikilinks]] → index/pages/
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


_SUFFIXES = re.compile(r"\b(servers?|services?|process(es)?|nodes?)\b$")


def concept(name: str) -> str:
    """Normalised concept key so "Namenode service" and "NameNode" meet: lowercase, no trailing server/service."""
    key = re.sub(r"\s*\(.*?\)", "", name.lower()).strip()
    key = _SUFFIXES.sub("", key).strip(" -_")
    key = re.sub(r"[^a-z0-9.+-]+", " ", key).strip()
    words = key.split()
    if words and len(words[-1]) > 4 and words[-1].endswith("s") and not words[-1].endswith("ss"):
        # plural → singular ("queries" → "query"), but leave short words/acronyms (EPS, DNS) alone
        words[-1] = words[-1][:-3] + "y" if words[-1].endswith("ies") else words[-1][:-1]
    return " ".join(words)


def concepts_of(e: Dict[str, Any]) -> Dict[str, str]:
    """concept key → display name, from components and keywords."""
    out: Dict[str, str] = {}
    for name in (e.get("components") or []) + (e.get("keywords") or []):
        k = concept(str(name))
        if k and k not in out:
            out[k] = str(name)
    return out


def related(index: "CaseIndex", ticket_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Other cases ranked by shared concepts (components count double), then recency."""
    target = index.get(ticket_id)
    if not target:
        return []
    comps = {concept(c) for c in target.get("components") or []}
    mine = set(concepts_of(target))
    ranked = []
    for e in index.entries():
        if e["ticket_id"] == target["ticket_id"]:
            continue
        theirs = set(concepts_of(e))
        shared = mine & theirs
        if shared:
            score = len(shared) + len(shared & comps & {concept(c) for c in e.get("components") or []})
            ranked.append((score, e.get("closed") or "", {**e, "shared": sorted(shared)}))
    ranked.sort(key=lambda r: (r[0], r[1]), reverse=True)
    return [e for _, _, e in ranked[:limit]]


STOPWORDS = {"the", "and", "for", "not", "are", "was", "were", "with", "from", "this", "that", "have", "has", "been",
             "able", "unable", "issue", "issues", "please", "kindly", "team", "check", "able", "into", "when", "there",
             "some", "only", "also", "after", "before", "getting", "showing", "working", "error", "request", "case"}


def _words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9.+-]{2,}", (text or "").lower()) if w not in STOPWORDS}


def similar(index: "CaseIndex", text: str, exclude: str = "", limit: int = 8, min_score: int = 3) -> List[Dict[str, Any]]:
    """Past cases that look like a new ticket (title + description), best first. Cheap and deterministic.

    A case's component or keyword appearing in the text scores 3; each shared title word scores 1.
    Used as the shortlist that Claude then checks, so it errs on the side of including.
    """
    norm = " " + re.sub(r"[^a-z0-9.+-]+", " ", (text or "").lower()) + " "
    words = _words(text)
    scored = []
    for e in index.entries():
        if e["ticket_id"] == exclude.upper():
            continue
        hits = [k for k in concepts_of(e) if len(k) > 2 and f" {k} " in norm]
        overlap = words & _words(e.get("title"))
        score = 3 * len(hits) + len(overlap)
        if score >= min_score:
            scored.append((score, e.get("closed") or "", {**e, "match": sorted(set(hits) | overlap)}))
    scored.sort(key=lambda r: (r[0], r[1]), reverse=True)
    return [e for _, _, e in scored[:limit]]


def _page_name(text: str) -> str:
    return re.sub(r"[\\/:*?\"<>|#^\[\]]+", " ", text).strip()[:80] or "unnamed"


def export_pages(index: "CaseIndex", out_dir: Path) -> Dict[str, int]:
    """One Markdown page per case plus one per component/customer/version, linked with [[wikilinks]].

    Readable as-is (Obsidian, any Markdown viewer), and the input for a graph tool.
    Rewritten from scratch each time, so it always matches the index.
    """
    import shutil
    for sub in ("cases", "concepts"):  # only our own folders: graphify-out/ etc. next to them survive
        if (out_dir / sub).exists():
            shutil.rmtree(out_dir / sub)
        (out_dir / sub).mkdir(parents=True)
    concept_cases: Dict[str, List[str]] = {}
    names: Dict[str, str] = {}
    seen: Dict[str, int] = {}
    for e in index.entries():
        for key in concepts_of(e):
            seen[key] = seen.get(key, 0) + 1
    for e in index.entries():
        links = []
        # Versions stay plain text: nearly every case has one of a few versions, so as graph links they
        # became the most-connected hubs and pulled unrelated cases into every query.
        if e.get("product_version"):
            links.append(f"version {e['product_version']}")
        for kind, value in (("customer", e.get("customer")),):
            if value:
                page = _page_name(f"{kind} {value}")
                links.append(f"[[{page}]]")
                concept_cases.setdefault(page, []).append(e["ticket_id"])
                names[page] = f"{kind.title()}: {value}"
        comp_links = []
        for key, display in concepts_of(e).items():
            if seen[key] < 2:  # only concepts shared by several cases get a page; the rest stay plain text
                comp_links.append(display)
                continue
            page = _page_name(key)
            concept_cases.setdefault(page, []).append(e["ticket_id"])
            names.setdefault(page, display)
            comp_links.append(f"[[{page}|{display}]]")
        body = [f"# {e['ticket_id']} · {e.get('title') or ''}", "",
                f"Closed {(e.get('closed') or '?')[:10]} · priority {e.get('priority') or '?'} · " + " · ".join(links), "",
                f"**Problem:** {e.get('problem') or '-'}", "", f"**Root cause:** {e.get('root_cause') or '-'}", "",
                f"**Fix:** {e.get('fix') or '-'}", "", "**Involves:** " + (", ".join(comp_links) or "-")]
        if e.get("jira_url"):
            body += ["", f"[Open in Jira]({e['jira_url']})"]
        (out_dir / "cases" / f"{e['ticket_id']}.md").write_text("\n".join(body) + "\n")
    for page, cases in concept_cases.items():
        lines = [f"# {names[page]}", "", "Cases:"] + [f"- [[{c}]]" for c in sorted(set(cases), reverse=True)]
        (out_dir / "concepts" / f"{page}.md").write_text("\n".join(lines) + "\n")
    return {"cases": len(index.entries()), "concepts": len(concept_cases)}


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
    rl = sub.add_parser("related")
    rl.add_argument("ticket_id")
    rl.add_argument("--limit", type=int, default=5)
    ex = sub.add_parser("export")
    ex.add_argument("--out", default=str(DEFAULT_INDEX_PATH.parent / "pages"))
    args = p.parse_args(argv)
    index = CaseIndex(Path(args.index))
    if args.cmd == "search":
        hits = index.search(args.query, args.limit)
        print("\n\n".join(format_entry(e) for e in hits) if hits else "No matching cases.")
    elif args.cmd == "show":
        e = index.get(args.ticket_id)
        print(format_entry(e) if e else f"{args.ticket_id.upper()} is not in the index.")
        return 0 if e else 1
    elif args.cmd == "related":
        hits = related(index, args.ticket_id, args.limit)
        if not index.get(args.ticket_id):
            print(f"{args.ticket_id.upper()} is not in the index.")
            return 1
        for e in hits:
            print(f"{e['ticket_id']:<11} {(e.get('closed') or '')[:10]}  shares: {', '.join(e['shared'])}\n"
                  f"            {e.get('title') or ''}")
        if not hits:
            print("No related cases yet.")
    elif args.cmd == "export":
        counts = export_pages(index, Path(args.out))
        print(f"Wrote {counts['cases']} case pages and {counts['concepts']} concept pages to {args.out}")
    else:
        for e in index.entries():
            print(f"{e['ticket_id']:<11} {(e.get('closed') or '')[:10]}  {e.get('customer') or '?':<14} {e.get('title') or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
