#!/usr/bin/env python3
"""
validate_hook.py -- bundled hook-definition schema check.

Reimplements, in pure Python, the structural checks that a hook definition must
pass before groundhog writes it (equivalent to hook-development's
validate-hook-schema.sh, which is unavailable here because jq is not installed).

Accepts either a full settings object (with a top-level ``hooks`` key) or a bare
hooks map. Checks event names, matcher/hook-array shape, and that every hook is a
``command`` or ``prompt`` hook with its required field (mirrors the upstream
validate-hook-schema.sh). With ``--config-dir`` it also warns when
``allowManagedHooksOnly`` would render the hook inert.

    python scripts/validate_hook.py path/to/hook.json [--config-dir DIR]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import groundhog_lib as fl

# Claude Code hook events (settings-level hooks).
VALID_EVENTS = {
    "PreToolUse", "PostToolUse", "UserPromptSubmit", "Notification",
    "Stop", "SubagentStop", "PreCompact", "SessionStart", "SessionEnd",
}
_MATCHER_EVENTS = {"PreToolUse", "PostToolUse"}  # matcher is meaningful here

# hookify-rule events (runtime-read markdown rules).
HOOKIFY_EVENTS = {"bash", "file", "stop", "prompt", "all"}

# Shell metacharacters / network fetchers worth a review flag on a generated
# command (mined command text is untrusted; a piped fetch is the classic risk).
_SHELL_METACHARS = re.compile(r"(?:[;&|`]|\$\(|\bcurl\b|\bwget\b|\bnc\b|>\s*/)")


def validate_hook_obj(data) -> list[dict]:
    findings: list[dict] = []

    def err(msg):
        findings.append({"level": "error", "msg": msg})

    def warn(msg):
        findings.append({"level": "warn", "msg": msg})

    if isinstance(data, dict) and isinstance(data.get("hooks"), dict):
        hooks_map = data["hooks"]
    elif isinstance(data, dict):
        hooks_map = data
    else:
        err("Top-level hook definition must be a JSON object.")
        return findings

    if not hooks_map:
        err("No hook events defined.")
        return findings

    for event, entries in hooks_map.items():
        if event not in VALID_EVENTS:
            err(f"Unknown hook event '{event}'. Valid: {', '.join(sorted(VALID_EVENTS))}.")
            continue
        if not isinstance(entries, list):
            err(f"Event '{event}' must map to a list of matcher groups.")
            continue
        for i, entry in enumerate(entries):
            where = f"{event}[{i}]"
            if not isinstance(entry, dict):
                err(f"{where} must be an object.")
                continue
            if "matcher" in entry and not isinstance(entry["matcher"], str):
                err(f"{where}.matcher must be a string.")
            if event in _MATCHER_EVENTS and not entry.get("matcher"):
                warn(f"{where} has no matcher; it will fire on ALL tools.")
            hook_list = entry.get("hooks")
            if not isinstance(hook_list, list) or not hook_list:
                err(f"{where}.hooks must be a non-empty list.")
                continue
            for j, hook in enumerate(hook_list):
                hw = f"{where}.hooks[{j}]"
                if not isinstance(hook, dict):
                    err(f"{hw} must be an object.")
                    continue
                htype = hook.get("type")
                if htype not in ("command", "prompt"):
                    err(f"{hw}.type must be 'command' or 'prompt' (got {htype!r}).")
                if htype == "command":
                    cmd = hook.get("command")
                    if not isinstance(cmd, str) or not cmd.strip():
                        err(f"{hw}.command must be a non-empty string.")
                    elif cmd.startswith("/") and "${CLAUDE_PLUGIN_ROOT}" not in cmd:
                        warn(f"{hw}.command uses a hardcoded absolute path.")
                    if isinstance(cmd, str) and _SHELL_METACHARS.search(cmd):
                        warn(f"{hw}.command contains shell metacharacters or a network "
                             "call; review it before installing (mined commands are "
                             "not trusted input).")
                elif htype == "prompt":
                    pr = hook.get("prompt")
                    if not isinstance(pr, str) or not pr.strip():
                        err(f"{hw}.prompt must be a non-empty string.")
                    if event not in ("Stop", "SubagentStop", "UserPromptSubmit", "PreToolUse"):
                        warn(f"{hw}: prompt hooks may not be supported on event '{event}'.")
                if "timeout" in hook:
                    t = hook["timeout"]
                    if not isinstance(t, int):
                        err(f"{hw}.timeout must be an integer (seconds).")
                    elif t > 600:
                        warn(f"{hw}.timeout {t}s is very high (max ~600s).")
                    elif t < 5:
                        warn(f"{hw}.timeout {t}s is very low.")
    return findings


def validate_hookify_text(text: str) -> list[dict]:
    """Validate a `.claude/hookify.<name>.local.md` runtime rule."""
    findings: list[dict] = []

    def err(msg):
        findings.append({"level": "error", "msg": msg})

    def warn(msg):
        findings.append({"level": "warn", "msg": msg})

    meta, body = fl.parse_frontmatter(text)
    if not meta:
        err("Hookify rule needs YAML frontmatter.")
        return findings
    if not meta.get("name"):
        err("Hookify rule missing 'name'.")
    if str(meta.get("enabled", "")).lower() not in ("true", "false"):
        err("Hookify rule 'enabled' must be true or false.")
    event = str(meta.get("event", ""))
    if event not in HOOKIFY_EVENTS:
        err(f"Hookify 'event' must be one of {sorted(HOOKIFY_EVENTS)} (got '{event}').")
    has_pattern = bool(meta.get("pattern")) or ("conditions:" in text)
    if not has_pattern:
        err("Hookify rule needs a 'pattern' or 'conditions:'.")
    if not body.strip():
        warn("Hookify rule has no message body (Claude sees nothing when it triggers).")
    action = str(meta.get("action", "warn"))
    if action not in ("warn", "block"):
        warn(f"Hookify 'action' should be warn or block (got '{action}').")
    return findings


def _print(findings, target) -> int:
    errs = [f for f in findings if f["level"] == "error"]
    warns = [f for f in findings if f["level"] == "warn"]
    print(f"validate_hook: {target}")
    for f in findings:
        mark = "ERROR" if f["level"] == "error" else "warn "
        print(f"  [{mark}] {f['msg']}")
    if not findings:
        print("  OK -- no issues.")
    print(f"  => {len(errs)} error(s), {len(warns)} warning(s)")
    return 1 if errs else 0


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Validate a hook definition JSON.")
    ap.add_argument("path", help="Path to a hook JSON file or a hookify .md rule.")
    ap.add_argument("--config-dir", help="Warn if allowManagedHooksOnly is active.")
    args = ap.parse_args(argv)

    path = Path(args.path)
    if path.suffix.lower() == ".md":
        try:
            findings = validate_hookify_text(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:  # ValueError covers UnicodeDecodeError
            print(f"validate_hook: cannot read/parse {args.path}: {e}", file=sys.stderr)
            return 1
    else:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"validate_hook: cannot read/parse {args.path}: {e}", file=sys.stderr)
            return 1
        findings = validate_hook_obj(data)

    if args.config_dir:
        cfg = fl.resolve_config_dir(args.config_dir)
        for fname in ("settings.json", "managed-settings.json", "remote-settings.json"):
            d = fl.read_json(cfg / fname)
            if isinstance(d, dict) and d.get("allowManagedHooksOnly") is True:
                findings.append({"level": "warn", "msg":
                    f"allowManagedHooksOnly is set ({fname}); a settings/plugin hook may be "
                    "INERT here. Prefer a runtime-read hookify rule."})
                break

    return _print(findings, args.path)


if __name__ == "__main__":
    raise SystemExit(_main())
