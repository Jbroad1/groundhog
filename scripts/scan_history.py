#!/usr/bin/env python3
"""
scan_history.py -- parse ~/.claude/history.jsonl into normalized prompt records.

Each line of history.jsonl is one submitted prompt::

    {"display": "...", "pastedContents": {...}, "timestamp": <epoch-ms>,
     "project": "C:\\path\\to\\repo"}

This is the cheapest high-signal source: prompt-intent clusters and slash-command
frequency. We normalize each ``display`` into a stable clustering key (volatile
operands masked, lowercased) and scrub secrets from the retained sample.

Importable (`scan_history(config_dir, since_ms)`) and runnable standalone.
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import groundhog_lib as fl
from scrub import scrub_text


def _norm_prompt(display: str) -> str:
    # template_bash masks URLs / quoted strings / paths / hashes / numbers and
    # collapses whitespace; lowercasing turns it into a clustering key.
    return fl.template_bash(display).lower()[:100]


def scan_history(config_dir: Path, since_ms: int | None = None) -> dict:
    path = config_dir / "history.jsonl"
    out = {"source": "history", "path": str(path), "exists": path.exists(),
           "total": 0, "count": 0, "prompts": []}
    if not path.exists():
        return out

    for _ln, rec in fl.iter_jsonl(path):
        if not isinstance(rec, dict) or "display" not in rec:
            continue
        out["total"] += 1
        ts = fl.to_epoch_ms(rec.get("timestamp"))
        if since_ms is not None and ts is not None and ts < since_ms:
            continue
        display = str(rec.get("display", "")).strip()
        if not display:
            continue
        project = str(rec.get("project", "") or "")
        is_slash = display.startswith("/")
        slash = display.split()[0] if is_slash else ""
        # Scrub FIRST, then normalize/sample, so no secret fragment survives
        # into the cluster key or the retained sample.
        scrubbed, _ = scrub_text(display)
        out["prompts"].append({
            "ts": ts,
            "project": project,
            "project_slug": fl.project_slug(project),
            "is_slash": is_slash,
            "slash": slash,
            "norm": _norm_prompt(scrubbed),
            "sample": scrubbed[:200],
        })
    out["count"] = len(out["prompts"])
    return out


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Scan history.jsonl.")
    ap.add_argument("--config-dir")
    ap.add_argument("--since", default=fl.RECENCY_DEFAULT)
    args = ap.parse_args(argv)
    cfg = fl.resolve_config_dir(args.config_dir)
    res = scan_history(cfg, fl.parse_since(args.since))
    print(f"history.jsonl: {res['count']}/{res['total']} prompts in window")
    slashes = Counter(p["slash"] for p in res["prompts"] if p["is_slash"])
    if slashes:
        print("top slash commands:", slashes.most_common(8))
    clusters = Counter(p["norm"] for p in res["prompts"])
    print("top prompt clusters:")
    for norm, n in clusters.most_common(8):
        print(f"  {n:>3}x  {norm[:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
