#!/usr/bin/env python3
"""Change → incident links: which closed service requests (changes) may have triggered later CASE incidents.

A change is an SR of a changing kind (upgrade, patching, new log source, extractor/parser, configuration,
storage) that was carried out. Its shortlist is the same customer's CASE tickets opened while it ran or within
30 days after it closed, that share a specific component/keyword with it or whose problem names that kind of
change ("after the upgrade", "unparsed logs" …). Generic concepts shared by most tickets ("dnif", "pico") don't
count. Claude then reads each change with its shortlist (one call per change) and marks every pair likely,
possible or unrelated with a short reason. Verdicts are cached in index/change_links.json (gitignored) with a
fingerprint of both tickets, so only new or changed pairs cost a call.

    python3 scripts/change_links.py candidates           # the shortlist, no Claude calls
    python3 scripts/change_links.py link --max-calls 20  # ask Claude about pairs not judged yet
    python3 scripts/change_links.py show [TICKET]        # likely/possible links (all, or one ticket's)

The links reach the graph pages ("Followed by" / "Possibly caused by", see case_index.export_pages), and a new
ticket's channel note lists the customer's recent changes (recent_changes).
"""
import argparse
import hashlib
import json
import math
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from case_index import CaseIndex, concepts_of, kind_of, _words  # noqa: E402

WINDOW_DAYS = 30
MAX_PER_CHANGE = 6        # cases sent to Claude with one change
GENERIC_SHARE = 0.10      # a concept on more than 10% of tickets says nothing about a link
DONE = ("completed", "partially completed")
VERDICTS = ("likely", "possible", "unrelated")
# The kinds of SR that change a customer's system, with the words a case uses when that kind of change hurt it.
CHANGE_CUES = {
    "upgrade": r"upgrad",
    "patching or maintenance": r"patch|reboot|restart|maintenance|kernel|os update",
    "new log source or integration": r"onboard|integrat|new (log )?source|connector|forwarder",
    "extractor or parser (incl. parsing issues)": r"extractor|parser|parsing|unparsed|\bparse",
    "configuration change": r"config|setting|migrat|changed?\b",
    "storage or retention": r"retention|storage|disk|archiv|purg",
}
CHANGE_CUES = {k: re.compile(v, re.I) for k, v in CHANGE_CUES.items()}


def _day(text: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat((text or "")[:10])
    except ValueError:
        return None


def is_change(e: Dict[str, Any]) -> bool:
    return kind_of(e) == "request" and e.get("category") in CHANGE_CUES and e.get("outcome") in DONE


def digest(e: Dict[str, Any]) -> str:
    body = {k: v for k, v in e.items() if k != "indexed_at"}
    return hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:12]


def _specific_weights(entries: List[Dict[str, Any]]) -> Dict[str, float]:
    """concept → weight (rarer is stronger); generic concepts are left out."""
    df: Dict[str, int] = {}
    for e in entries:
        for k in concepts_of(e):
            df[k] = df.get(k, 0) + 1
    n = max(len(entries), 1)
    return {k: math.log(n / d) for k, d in df.items() if d <= max(2, GENERIC_SHARE * n)}


def candidates(index: CaseIndex, window_days: int = WINDOW_DAYS) -> List[Dict[str, Any]]:
    """Every (change, incident) pair worth asking about, best first within each change."""
    entries = index.entries()
    weights = _specific_weights(entries)
    cases = [e for e in entries if kind_of(e) == "case" and _day(e.get("opened"))]
    out = []
    for sr in entries:
        if not is_change(sr):
            continue
        start = _day(sr.get("opened"))
        end = (_day(sr.get("closed")) or start) + timedelta(days=window_days) if start else None
        if not start:
            continue
        cue = CHANGE_CUES[sr["category"]]
        mine = set(concepts_of(sr))
        pairs = []
        for c in cases:
            if (c.get("customer") or "").strip().lower() != (sr.get("customer") or "").strip().lower():
                continue
            opened = _day(c["opened"])
            if not start <= opened <= end:
                continue
            shared = sorted(k for k in mine & set(concepts_of(c)) if k in weights)
            text = " ".join(c.get(f) or "" for f in ("title", "problem", "root_cause"))
            cued = bool(cue.search(text))
            score = sum(weights[k] for k in shared) + (2.0 if cued else 0.0)
            if shared or cued:
                closed = _day(sr.get("closed")) or start
                pairs.append({"change": sr["ticket_id"], "incident": c["ticket_id"], "shared": shared, "cue": cued,
                              "score": round(score, 2), "days_after": (opened - closed).days})
        pairs.sort(key=lambda p: (-p["score"], p["incident"]))
        out += pairs[:MAX_PER_CHANGE]
    return out


class LinkStore:
    """index/change_links.json: "SR-1>CASE-2" → {verdict, why, fp, checked}."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.pairs: Dict[str, Dict[str, Any]] = {}
        if self.path.exists():
            try:
                self.pairs = json.loads(self.path.read_text()).get("pairs", {})
            except (OSError, ValueError):
                self.pairs = {}

    @classmethod
    def beside(cls, index: CaseIndex) -> "LinkStore":
        return cls(index.path.parent / "change_links.json")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pairs": self.pairs}, indent=1, sort_keys=True))
        os.replace(tmp, self.path)


def _pair_key(p: Dict[str, Any]) -> str:
    return f"{p['change']}>{p['incident']}"


def _pair_fp(index: CaseIndex, p: Dict[str, Any]) -> str:
    return digest(index.get(p["change"]) or {}) + digest(index.get(p["incident"]) or {})


def pending(index: CaseIndex, store: LinkStore, pairs: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """change → its pairs with no verdict for the current text of both tickets."""
    todo: Dict[str, List[Dict[str, Any]]] = {}
    for p in pairs:
        known = store.pairs.get(_pair_key(p))
        if not known or known.get("fp") != _pair_fp(index, p):
            todo.setdefault(p["change"], []).append(p)
    return todo


def link(index: CaseIndex, judge, store: Optional[LinkStore] = None, max_calls: Optional[int] = None,
         window_days: int = WINDOW_DAYS) -> Dict[str, int]:
    """Ask `judge(change_entry, [case entries])` → {ticket_id: {verdict, why}} (or None on failure) about every
    change with unjudged pairs, at most max_calls changes per run. Pairs no longer shortlisted are dropped."""
    store = store or LinkStore.beside(index)
    pairs = candidates(index, window_days)
    live = {_pair_key(p) for p in pairs}
    dropped = [k for k in store.pairs if k not in live]
    for k in dropped:
        del store.pairs[k]
    todo = pending(index, store, pairs)
    calls = failed = judged = 0
    today = datetime.now(timezone.utc).date().isoformat()
    for change, group in sorted(todo.items()):
        if max_calls is not None and calls >= max_calls:
            break
        calls += 1
        verdicts = judge(index.get(change), [index.get(p["incident"]) for p in group])
        if verdicts is None:
            failed += 1
            continue
        for p in group:
            v = verdicts.get(p["incident"]) or {}
            verdict = v.get("verdict") if v.get("verdict") in VERDICTS else "unrelated"
            why = str(v.get("why") or "").strip()
            why = why if len(why) <= 200 else why[:200].rsplit(" ", 1)[0] + "…"
            store.pairs[_pair_key(p)] = {"verdict": verdict, "why": why,
                                         "fp": _pair_fp(index, p), "checked": today}
            judged += 1
    if calls or dropped:
        store.save()
    left = sum(len(g) for g in pending(index, store, pairs).values())
    return {"candidates": len(pairs), "calls": calls, "failed": failed, "judged": judged, "dropped": len(dropped),
            "pending": left}


def links(index: CaseIndex, store: Optional[LinkStore] = None, include_possible: bool = True) -> List[Dict[str, Any]]:
    """Judged links worth showing, likely first, then by date."""
    store = store or LinkStore.beside(index)
    keep = ("likely", "possible") if include_possible else ("likely",)
    out = []
    for key, v in store.pairs.items():
        change, incident = key.split(">")
        if v.get("verdict") in keep and index.get(change) and index.get(incident):
            out.append({"change": change, "incident": incident, "verdict": v["verdict"], "why": v.get("why") or ""})
    out.sort(key=lambda l: (l["verdict"] != "likely", (index.get(l["incident"]) or {}).get("opened") or ""), reverse=False)
    return out


def by_ticket(index: CaseIndex, store: Optional[LinkStore] = None) -> Dict[str, List[Dict[str, Any]]]:
    """ticket → its links (both ends), for the graph pages."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for l in links(index, store):
        out.setdefault(l["change"], []).append(l)
        out.setdefault(l["incident"], []).append(l)
    return out


def recent_changes(index: CaseIndex, customer: str, text: str = "", today: Optional[date] = None,
                   days: int = WINDOW_DAYS, limit: int = 3) -> List[Dict[str, Any]]:
    """This customer's changes that closed in the last `days` days, those touching the new ticket's words first."""
    if not (customer or "").strip():
        return []
    today = today or datetime.now(timezone.utc).date()
    words = _words(text)
    hits = []
    for e in index.entries():
        closed = _day(e.get("closed"))
        if not (is_change(e) and closed and (e.get("customer") or "").strip().lower() == customer.strip().lower()
                and 0 <= (today - closed).days <= days):
            continue
        overlap = len(words & (_words(e.get("title")) | {w for k in concepts_of(e) for w in _words(k)}))
        hits.append((overlap, closed, {**e, "days_ago": (today - closed).days}))
    hits.sort(key=lambda h: (h[0], h[1]), reverse=True)
    return [e for _, _, e in hits[:limit]]


def default_judge():
    """Claude through the CLI, with the model from config/config.yaml; None if summaries are off."""
    import yaml
    from summarizer import from_config
    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "config" / "config.yaml").read_text())
    s = from_config(cfg.get("tier_2", {}).get("sync", {}))
    return s.judge_changes if s else None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Link closed changes (SRs) to the incidents (CASEs) that followed them.")
    p.add_argument("--window", type=int, default=WINDOW_DAYS, help=f"days after a change to look (default {WINDOW_DAYS})")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("candidates", help="the shortlist, without asking Claude")
    lk = sub.add_parser("link", help="ask Claude about pairs not judged yet")
    lk.add_argument("--max-calls", type=int, default=None)
    sh = sub.add_parser("show", help="likely and possible links")
    sh.add_argument("ticket", nargs="?", default="")
    sh.add_argument("--likely", action="store_true", help="only likely links")
    args = p.parse_args(argv)
    index = CaseIndex()
    if args.cmd == "candidates":
        pairs = candidates(index, args.window)
        for c in pairs:
            print(f"{c['change']:<9} → {c['incident']:<10} score {c['score']:>5}  {c['days_after']:+4}d  "
                  f"{'cue ' if c['cue'] else ''}{', '.join(c['shared'][:4])}")
        print(f"{len(pairs)} pairs from {len({c['change'] for c in pairs})} changes; "
              f"{sum(len(g) for g in pending(index, LinkStore.beside(index), pairs).values())} not judged yet")
    elif args.cmd == "link":
        judge = default_judge()
        if not judge:
            print("Summaries are off in config/config.yaml; nothing to ask.", file=sys.stderr)
            return 1
        r = link(index, judge, max_calls=args.max_calls, window_days=args.window)
        print(f"{r['candidates']} pairs · {r['calls']} Claude calls ({r['failed']} failed) · {r['judged']} judged · "
              f"{r['pending']} still pending")
        return 1 if r["failed"] else 0
    else:
        shown = [l for l in links(index, include_possible=not args.likely)
                 if not args.ticket or args.ticket.upper() in (l["change"], l["incident"])]
        for l in shown:
            sr, case = index.get(l["change"]), index.get(l["incident"])
            print(f"{l['change']} ({sr.get('category')}, closed {(sr.get('closed') or '?')[:10]}) → "
                  f"{l['incident']} (opened {(case.get('opened') or '?')[:10]}) · {sr.get('customer') or '?'} · "
                  f"{l['verdict']}\n  {sr.get('title') or ''}\n  → {case.get('title') or ''}\n  why: {l['why']}\n")
        print(f"{len(shown)} link{'s' if len(shown) != 1 else ''}" + (f" for {args.ticket.upper()}" if args.ticket else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
