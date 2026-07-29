#!/usr/bin/env python3
"""
preflight.py -- environment + policy detection for groundhog (Phase 0).

Resolves the Claude config dir portably, detects the platform / Python / jq, the
``allowManagedHooksOnly`` managed-hooks policy, and the presence of optional
skills we can lean on if installed (but never require). Emits ``env.json``.

The ``allow_managed_hooks_only`` flag it returns is threaded into the miner so
hook candidates degrade to runtime-read hookify rules with a warning when a
settings-level hook would be silently inert.

Pure stdlib. Importable (`preflight(config_dir)`) and runnable standalone.
"""
from __future__ import annotations

import argparse
import json
import platform
import shutil
import sys
from pathlib import Path

import groundhog_lib as fl

# Optional skills we detect + reuse if present; each has a bundled fallback so
# groundhog runs standalone without any of them.
_OPTIONAL_SKILLS = [
    "continuous-learning-v2",   # secret-scrub conventions / observations schema
    "skill-comply",             # independent review grader (compile phase)
    "skill-stocktake",          # independent review grader (compile phase)
    "skill-creator",            # authoring method (compile phase)
    "skill-scout",              # dedup / discovery of existing skills
    "writing-skills",           # authoring method (we bundle our own copy too)
    "hookify-rules",            # runtime-read hook rule format
    "automation-audit-ops",     # evidence-first vocabulary
    "agent-introspection-debugging",  # loop-detection heuristic
]


def _find_skill(config_dir: Path, name: str) -> str | None:
    direct = config_dir / "skills" / name / "SKILL.md"
    if direct.exists():
        return str(direct)
    plugins = config_dir / "plugins"
    if plugins.is_dir():
        # bounded search: skill dirs are shallow inside plugin caches
        for p in plugins.rglob(f"{name}/SKILL.md"):
            return str(p)
    return None


def _managed_hooks_policy(config_dir: Path) -> dict:
    """Return {'active': bool, 'source': filename|None}. Reads the settings
    files most-specific-last so a managed policy wins."""
    result = {"active": False, "source": None}
    for fname in ("settings.json", "settings.local.json", "managed-settings.json",
                  "remote-settings.json"):
        data = fl.read_json(config_dir / fname)
        if isinstance(data, dict) and data.get("allowManagedHooksOnly") is True:
            result = {"active": True, "source": fname}
    return result


def preflight(config_dir: Path) -> dict:
    policy = _managed_hooks_policy(config_dir)
    hist = config_dir / "history.jsonl"
    projects = config_dir / "projects"
    plans = config_dir / "plans"

    env = {
        "schema_version": fl.SCHEMA_VERSION,
        "config_dir": str(config_dir),
        "config_dir_exists": config_dir.is_dir(),
        "platform": platform.system(),
        "platform_release": platform.release(),
        "python_version": platform.python_version(),
        "jq_available": shutil.which("jq") is not None,
        "allow_managed_hooks_only": policy["active"],
        "managed_policy_source": policy["source"],
        "data": {
            "history_exists": hist.exists(),
            "history_bytes": hist.stat().st_size if hist.exists() else 0,
            "project_dirs": sum(1 for _ in projects.glob("*/")) if projects.is_dir() else 0,
            "transcript_files": sum(1 for _ in projects.rglob("*.jsonl")) if projects.is_dir() else 0,
            "plan_files": sum(1 for _ in plans.glob("*.md")) if plans.is_dir() else 0,
        },
        "optional_skills": {name: _find_skill(config_dir, name) for name in _OPTIONAL_SKILLS},
        "warnings": [],
    }

    if not env["config_dir_exists"]:
        env["warnings"].append(f"Config dir not found: {config_dir}")
    if not env["jq_available"]:
        env["warnings"].append("jq not installed -- groundhog uses pure-Python parsing (fine).")
    if env["allow_managed_hooks_only"]:
        env["warnings"].append(
            "allowManagedHooksOnly is set (source: %s): settings/plugin-level hooks may be "
            "INERT on this machine. Emitted hook guardrails will fall back to runtime-read "
            "hookify rules where possible." % policy["source"])
    if not env["data"]["history_exists"] and env["data"]["transcript_files"] == 0:
        env["warnings"].append("No history.jsonl and no transcripts found -- nothing to mine.")
    return env


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="groundhog preflight / env detection.")
    ap.add_argument("--config-dir")
    ap.add_argument("--out", help="Write env.json here.")
    ap.add_argument("--json", action="store_true", help="Print env.json to stdout.")
    args = ap.parse_args(argv)

    cfg = fl.resolve_config_dir(args.config_dir)
    env = preflight(cfg)
    if args.out:
        fl.write_json(Path(args.out), env)
    if args.json or not args.out:
        json.dump(env, sys.stdout, indent=2)
        print()
    for w in env["warnings"]:
        print("  [warn]", w, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
