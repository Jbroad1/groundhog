#!/usr/bin/env python3
"""
validate_skill.py -- bundled SKILL.md frontmatter validator.

Enforces the writing-skills authoring rules on any groundhog emits (and on
groundhog itself), so no invalid skill ever lands:

  * frontmatter present with required `name` and `description`,
  * `name` uses letters / numbers / hyphens only,
  * total frontmatter <= 1024 chars,
  * `description` starts with "Use when", is third-person, and does NOT
    summarize the workflow (a summary makes agents skip the body),
  * body has an H1 heading.

Original implementation. Rules follow superpowers:writing-skills. Pure stdlib.

    python scripts/validate_skill.py path/to/skill-dir-or-SKILL.md
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import groundhog_lib as fl

_NAME_RE = re.compile(r"^[A-Za-z0-9-]+$")
_FIRST_PERSON = re.compile(r"\b(I|I'll|I'm|we|we'll|my|our)\b", re.IGNORECASE)
# tokens that usually mean the description is describing the *process*, not the trigger
_WORKFLOW_HINTS = (" then ", "->", " step ", "first,", " dispatches ", " runs ", " parses ")


def validate_skill(path: Path) -> list[dict]:
    findings: list[dict] = []

    def err(msg):
        findings.append({"level": "error", "msg": msg})

    def warn(msg):
        findings.append({"level": "warn", "msg": msg})

    skill_md = path / "SKILL.md" if path.is_dir() else path
    if not skill_md.exists():
        err(f"SKILL.md not found at {skill_md}")
        return findings

    text = skill_md.read_text(encoding="utf-8", errors="replace").lstrip("﻿")
    if not text.startswith("---"):
        err("Missing YAML frontmatter (file must start with '---').")
        return findings

    # measure the raw frontmatter block length
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if m and len(m.group(1)) > 1024:
        err(f"Frontmatter is {len(m.group(1))} chars (max 1024).")

    meta, body = fl.parse_frontmatter(text)
    name = meta.get("name", "")
    desc = meta.get("description", "")

    if not name:
        err("Frontmatter missing required field: name")
    elif not _NAME_RE.match(str(name)):
        err(f"name '{name}' must use only letters, numbers, and hyphens.")

    if not desc:
        err("Frontmatter missing required field: description")
    else:
        desc = str(desc)
        if not desc.lower().startswith("use when"):
            warn("description should start with 'Use when' (triggering conditions only).")
        if len(desc) > 500:
            warn(f"description is {len(desc)} chars; keep under 500 for discovery.")
        if _FIRST_PERSON.search(desc):
            warn("description should be third-person (injected into the system prompt).")
        low = " " + desc.lower() + " "
        if any(h in low for h in _WORKFLOW_HINTS):
            warn("description looks like it summarizes the workflow; describe ONLY when to "
                 "use it, or agents will skip the body.")

    if not re.search(r"^#\s+\S", body, re.MULTILINE):
        warn("Body has no H1 heading (expected '# Skill Name').")

    return findings


def _print(findings, target) -> int:
    errs = [f for f in findings if f["level"] == "error"]
    warns = [f for f in findings if f["level"] == "warn"]
    print(f"validate_skill: {target}")
    for f in findings:
        mark = "ERROR" if f["level"] == "error" else "warn "
        print(f"  [{mark}] {f['msg']}")
    if not findings:
        print("  OK -- no issues.")
    print(f"  => {len(errs)} error(s), {len(warns)} warning(s)")
    return 1 if errs else 0


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Validate a SKILL.md.")
    ap.add_argument("path", help="Skill directory or SKILL.md path.")
    args = ap.parse_args(argv)
    findings = validate_skill(Path(args.path))
    return _print(findings, args.path)


if __name__ == "__main__":
    raise SystemExit(_main())
