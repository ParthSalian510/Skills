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
# What each kind of entry records: a CASE is a fault (problem → cause → fix), an SR is a piece of requested
# work (request → category → work done → outcome). Messages, search, pages and the CLI all read this.
ENTRY_SECTIONS = {
    "case": [("problem", "Problem"), ("root_cause", "Root cause"), ("fix", "Fix")],
    "request": [("request", "Request"), ("category", "Category"), ("work_done", "Work done"),
                ("outcome", "Outcome"), ("blockers", "Blockers")],
}


def kind_of(e: Dict[str, Any]) -> str:
    return "request" if e.get("kind") == "request" else "case"


def sections(e: Dict[str, Any]):
    """(field, label) pairs for this entry's kind, in display order."""
    return ENTRY_SECTIONS[kind_of(e)]


def resolution_text(e: Dict[str, Any]) -> str:
    """The one line that says how it ended: a case's fix, or a request's work done and outcome."""
    if kind_of(e) == "request":
        done = e.get("work_done") or "Nothing recorded"
        return f"{done} ({e['outcome']})" if e.get("outcome") else done
    return e.get("fix") or "Not recorded"


SEARCH_FIELDS = {"title": 3, "problem": 3, "root_cause": 3, "fix": 2, "components": 3, "keywords": 3,
                 "request": 3, "category": 2, "work_done": 2, "blockers": 1,
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


def _without_customers(text: str, own: str, customers: List[str]) -> str:
    """Drop customer names from page text. A leading "ACME || " goes; elsewhere the case's own customer
    becomes "the customer" and any other known customer "another customer"."""
    if not text:
        return text
    for c in customers:  # longest first, so "ACME Labs" goes before a shorter name inside it
        name = re.escape(c)
        text = re.sub(rf"^\s*{name}s?\s*[|:\-–]+\s*", "", text, flags=re.I)
        swap = "the customer" if c.lower() == own.lower() else "another customer"
        text = re.sub(rf"(?<!\w)(?:the\s+)?{name}s?(?!\w)", swap, text, flags=re.I)
    return text


def export_pages(index: "CaseIndex", out_dir: Path) -> Dict[str, int]:
    """One Markdown page per case plus one per shared component/keyword, linked with [[wikilinks]].

    Readable as-is (Obsidian, any Markdown viewer), and the input for a graph tool.
    Rewritten from scratch each time, so it always matches the index.
    """
    import shutil
    for sub in ("cases", "requests", "concepts"):  # only our own folders: graphify-out/ etc. next to them survive
        if (out_dir / sub).exists():
            shutil.rmtree(out_dir / sub)
        (out_dir / sub).mkdir(parents=True)
    concept_cases: Dict[str, List[str]] = {}
    names: Dict[str, str] = {}
    seen: Dict[str, int] = {}
    for e in index.entries():
        for key in concepts_of(e):
            seen[key] = seen.get(key, 0) + 1
    customers = sorted({(e.get("customer") or "").strip() for e in index.entries()} - {""}, key=len, reverse=True)
    named = re.compile("|".join(rf"(?<!\w){re.escape(c)}(?!\w)" for c in customers), re.I) if customers else None
    for e in index.entries():
        # No customer on the pages: even as plain text, Graphify's semantic pass made each customer a hub
        # (one linked 28 cases), so topics grouped by customer instead of by kind of problem. The name stays
        # in index/cases.jsonl, search and #case-index; only the graph input leaves it out.
        cust = (e.get("customer") or "").strip()
        text = {k: _without_customers(e.get(k) or "", cust, customers)
                for k in ["title"] + [f for f, _ in sections(e)]}
        links = [f"version {e['product_version']}"] if e.get("product_version") else []
        comp_links = []
        for key, display in concepts_of(e).items():
            # only concepts shared by several cases get a page, and never one named after a customer
            if seen[key] < 2 or (named and named.search(key)):
                comp_links.append(_without_customers(display, cust, customers))
                continue
            page = _page_name(key)
            concept_cases.setdefault(page, []).append(e["ticket_id"])
            names.setdefault(page, display)
            comp_links.append(f"[[{page}|{display}]]")
        kind = kind_of(e)
        if kind == "request" and e.get("category"):
            # a fixed link written by this code (not left to Graphify's guesswork): every request joins its category
            page = _page_name(f"request {e['category']}")
            concept_cases.setdefault(page, []).append(e["ticket_id"])
            names.setdefault(page, f"Request type: {e['category']}")
            text["category"] = f"[[{page}|{e['category']}]]"
        body = [f"# {e['ticket_id']} · {text['title']}", "",
                " · ".join([("Service request · " if kind == "request" else "") + f"Closed {(e.get('closed') or '?')[:10]}",
                            f"priority {e.get('priority') or '?'}"] + links), ""]
        for field, label in sections(e):
            if field == "blockers" and not text.get(field):
                continue
            body += [f"**{label}:** {text.get(field) or '-'}", ""]
        body.append("**Involves:** " + (", ".join(comp_links) or "-"))
        if e.get("jira_url"):
            body += ["", f"[Open in Jira]({e['jira_url']})"]
        folder = "requests" if kind == "request" else "cases"
        (out_dir / folder / f"{e['ticket_id']}.md").write_text("\n".join(body) + "\n")
    for page, cases in concept_cases.items():
        lines = [f"# {names[page]}", "", "Cases:"] + [f"- [[{c}]]" for c in sorted(set(cases), reverse=True)]
        (out_dir / "concepts" / f"{page}.md").write_text("\n".join(lines) + "\n")
    kinds = [kind_of(e) for e in index.entries()]
    return {"cases": kinds.count("case"), "requests": kinds.count("request"), "concepts": len(concept_cases)}


# Short names the team uses for components, so a topic like "dn query" finds "Datanode … slow queries".
TOPIC_ALIASES = {"dn": ["dn", "datanode", "data node"], "co": ["co", "core"], "ad": ["ad", "adapter"],
                 "query": ["query", "queries", "search"], "sww": ["sww", "something went wrong"]}


def _matches_topic(e: Dict[str, Any], topic: str) -> bool:
    """Every word of the topic (or one of its aliases) appears in the case's text."""
    if not topic:
        return True
    text = " ".join(str(e.get(k) or "") for k in ("title", "problem", "root_cause", "fix", "request", "work_done"))
    text = (text + " " + " ".join(e.get("components") or []) + " " + " ".join(e.get("keywords") or [])).lower()
    for word in topic.lower().split():
        options = TOPIC_ALIASES.get(word, [word])
        if not any(re.search(rf"\b{re.escape(o)}(e?s)?\b", text) for o in options):
            return False
    return True


def _side(index: "CaseIndex", who: str, topic: str) -> List[str]:
    """Ticket keys for one side: a single ticket key, or every case of a customer (name match, any case)."""
    if re.fullmatch(r"[A-Za-z]+-\d+", who.strip()):
        return [who.strip().upper()] if index.get(who.strip().upper()) else []
    w = who.strip().lower()
    return sorted(e["ticket_id"] for e in index.entries()
                  if w and w in (e.get("customer") or "").lower() and _matches_topic(e, topic))


def _case_node(nodes: List[Dict[str, Any]], key: str) -> Optional[str]:
    """The graph node for a case page: its label starts with the key ("CASE-1: …"), never CASE-10's."""
    rx = re.compile(rf"^{re.escape(key)}(?!\d)")
    return next((n["id"] for n in nodes if rx.match(str(n.get("label") or ""))), None)


def connect(index: "CaseIndex", graph_path: Path, side_a: str, side_b: str, topic_a: str = "", topic_b: str = "",
            limit: int = 10) -> Dict[str, Any]:
    """Shortest paths in the Graphify graph between two customers' cases (or single tickets), shortest first.

    Customers are not nodes in the graph (names are kept off the pages), so the private index maps each
    customer to its case nodes and the paths run between those. Plain breadth-first search, no dependencies.
    """
    graph = json.loads(Path(graph_path).read_text())
    nodes = graph.get("nodes") or []
    labels = {n["id"]: str(n.get("label") or n["id"]) for n in nodes}
    adj: Dict[str, List[tuple]] = {}
    for l in graph.get("links") or graph.get("edges") or []:
        rel = l.get("relation") or "related"
        adj.setdefault(l["source"], []).append((l["target"], rel))
        adj.setdefault(l["target"], []).append((l["source"], rel))
    a_keys, b_keys = _side(index, side_a, topic_a), _side(index, side_b, topic_b)
    b_nodes = {}
    for k in b_keys:
        n = _case_node(nodes, k)
        if n:
            b_nodes[n] = k
    paths = []
    for a in a_keys:
        start = _case_node(nodes, a)
        if not start:
            continue
        prev = {start: None}
        queue = [start]
        for node in queue:  # BFS: queue grows as we go
            for nxt, rel in adj.get(node, []):
                if nxt not in prev:
                    prev[nxt] = (node, rel)
                    queue.append(nxt)
        for bn, b in b_nodes.items():
            if bn not in prev or b == a:
                continue
            chain, rels, cur = [bn], [], bn
            while prev[cur]:
                cur, rel = prev[cur]
                chain.append(cur)
                rels.append(rel)
            chain.reverse()
            rels.reverse()
            paths.append({"a": a, "b": b, "hops": len(chain) - 1, "nodes": [labels[n] for n in chain], "relations": rels})
    paths.sort(key=lambda p: (p["hops"], p["a"], p["b"]))
    return {"a": a_keys, "b": b_keys, "paths": paths[:limit] if limit else paths}


def format_entry(e: Dict[str, Any]) -> str:
    """Plain-text view for the CLI."""
    lines = [f"{e['ticket_id']} · {e.get('customer') or '?'} · {e.get('product_version') or 'no version'} · "
             f"{e.get('priority') or '?'} · closed {(e.get('closed') or '?')[:10]}",
             f"  {e.get('title') or ''}"]
    lines += [f"  {label + ':':<11} {e.get(field) or '-'}" for field, label in sections(e)]
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
    cn = sub.add_parser("connect", help="shortest graph paths between two customers' cases (or two tickets)")
    cn.add_argument("side_a", help="customer name (or part of it) or a ticket key")
    cn.add_argument("side_b", help="customer name (or part of it) or a ticket key")
    cn.add_argument("--topic-a", default="", help="only side A cases about this, e.g. \"dn memory\"")
    cn.add_argument("--topic-b", default="", help="only side B cases about this, e.g. \"dn query\"")
    cn.add_argument("--limit", type=int, default=6)
    cn.add_argument("--graph", default=str(DEFAULT_INDEX_PATH.parent / "pages" / "graphify-out" / "graph.json"))
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
    elif args.cmd == "connect":
        if not Path(args.graph).exists():
            print(f"No graph at {args.graph}; build it with scripts/rebuild_graph.py.", file=sys.stderr)
            return 1
        r = connect(index, Path(args.graph), args.side_a, args.side_b, args.topic_a, args.topic_b, args.limit)
        def side(name, keys, topic):
            return f"{name}{' (' + topic + ')' if topic else ''}: {len(keys)} case{'s' if len(keys) != 1 else ''}"
        print(f"{side(args.side_a, r['a'], args.topic_a)} · {side(args.side_b, r['b'], args.topic_b)} · "
              f"{len(r['paths'])} closest connection{'s' if len(r['paths']) != 1 else ''} shown")
        if not r["paths"]:
            print("No connection found (check the names, or loosen --topic).")
            return 0
        best = r["paths"][0]["hops"]
        for p in r["paths"]:
            chain = p["nodes"][0] + "".join(f"\n     ── {rel} ──▶ {n}" for rel, n in zip(p["relations"], p["nodes"][1:]))
            print(f"\n{p['a']} → {p['b']} · {p['hops']} hop{'s' if p['hops'] != 1 else ''}\n  {chain}")
            if p["hops"] == best:  # the closest pairs: show what each case was and how it ended
                for key in (p["a"], p["b"]):
                    e = index.get(key) or {}
                    first = sections(e)[0]
                    clip = lambda t: (t or "-") if len(t or "") <= 220 else t[:220].rsplit(" ", 1)[0] + "…"
                    print(f"  {key} · {e.get('customer') or '?'} · {e.get('product_version') or 'no version'}\n"
                          f"    {first[1]}: {clip(e.get(first[0]))}\n"
                          f"    {'Fix' if kind_of(e) == 'case' else 'Done'}: {clip(resolution_text(e))}")
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
        print(f"Wrote {counts['cases']} case pages, {counts['requests']} request pages and "
              f"{counts['concepts']} concept pages to {args.out}")
    else:
        for e in index.entries():
            print(f"{e['ticket_id']:<11} {(e.get('closed') or '')[:10]}  {e.get('customer') or '?':<14} {e.get('title') or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
