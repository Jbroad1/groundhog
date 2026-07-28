#!/usr/bin/env python3
"""
scan_plans.py -- parse ~/.claude/plans/*.md (green-lit multi-step plans).

Plans capture the recurring *types* of multi-step work you approve. We extract
each plan's title, section headings, and a rough step count, and derive a
normalized title so recurring plan shapes cluster together (a strong signal for
skill-chain candidates).

Importable (`scan_plans(...)`) and runnable standalone.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import groundhog_lib as fl
from scrub import scrub_text

_STEP_RE = re.compile(r"^\s*(?:\d+[.)]|[-*]\s|#{2,4}\s*(?:step|phase|stage))", re.IGNORECASE)
_H1_RE = re.compile(r"^#\s+(.*)$")
_H2_RE = re.compile(r"^#{2,3}\s+(.*)$")


def _parse_plan(path: Path) -> dict:
    title = path.stem
    headings: list[str] = []
    n_steps = 0
    words = 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    got_title = False
    for line in text.splitlines():
        words += len(line.split())
        if not got_title:
            m = _H1_RE.match(line)
            if m:
                title = m.group(1).strip()
                got_title = True
                continue
        m2 = _H2_RE.match(line)
        if m2 and len(headings) < 30:
            headings.append(m2.group(1).strip())
        if _STEP_RE.match(line):
            n_steps += 1
    sample, _ = scrub_text(title[:160])
    return {
        "file": str(path),
        "title": sample,
        "norm": fl.template_bash(title).lower()[:80],
        "headings": [scrub_text(h[:120])[0] for h in headings],
        "n_steps": n_steps,
        "words": words,
    }


def scan_plans(config_dir: Path, since_ms: int | None = None) -> dict:
    root = config_dir / "plans"
    out = {"source": "plans", "root": str(root), "exists": root.is_dir(),
           "total": 0, "count": 0, "plans": []}
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*.md")):
        out["total"] += 1
        try:
            mtime_ms = int(path.stat().st_mtime * 1000)
        except OSError:
            continue
        if since_ms is not None and mtime_ms < since_ms:
            continue
        out["plans"].append(_parse_plan(path))
    out["count"] = len(out["plans"])
    return out


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Scan plans/*.md.")
    ap.add_argument("--config-dir")
    ap.add_argument("--since", default=fl.RECENCY_DEFAULT)
    args = ap.parse_args(argv)
    cfg = fl.resolve_config_dir(args.config_dir)
    res = scan_plans(cfg, fl.parse_since(args.since))
    print(f"plans: {res['count']}/{res['total']} in window")
    for p in res["plans"][:10]:
        print(f"  [{p['n_steps']:>2} steps] {p['title'][:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
