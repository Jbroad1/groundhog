#!/usr/bin/env python3
"""
groundhog.py -- groundhog CLI orchestrator (the deterministic backbone).

Sub-commands:

  preflight            Detect env + managed-hooks policy + optional skills.
  scan                 Preflight + mine ~/.claude into a scored scan.json.
  validate-skill PATH  Validate a SKILL.md.
  validate-hook PATH   Validate a hook definition JSON.
  package DIR          Zip a skill directory.
  install --plan P     Write approved artifacts (skills / hookify rules / hooks).

Autonomy model (review-by-default):
  * `scan` only ever writes analysis artifacts (scan.json, cache) -- never an
    automation. Safe to run anytime.
  * `install` PREVIEWS by default (no writes). It writes only with `--yes`.
    `--dry-run` forces preview even with `--yes`.

The LLM phases (analyze, compile) are driven by SKILL.md, not this script; groundhog
provides the deterministic building blocks they call.

Pure stdlib.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import groundhog_lib as fl
from preflight import preflight
from mine_workflows import mine
from validate_skill import validate_skill, _print as _print_skill
from validate_hook import validate_hook_obj, validate_hookify_text, _print as _print_hook
from package_skill import package_skill


def _run_dir(cfg: Path) -> Path:
    return fl.ensure_dir(fl.state_dir(cfg) / "last-run")


# --------------------------------------------------------------------------- #
def cmd_preflight(args) -> int:
    cfg = fl.resolve_config_dir(args.config_dir)
    env = preflight(cfg)
    fl.write_json(_run_dir(cfg) / "env.json", env)
    json.dump(env, sys.stdout, indent=2)
    print()
    for w in env["warnings"]:
        print("  [warn]", w, file=sys.stderr)
    return 0


def cmd_scan(args) -> int:
    cfg = fl.resolve_config_dir(args.config_dir)
    env = preflight(cfg)
    cache_path = fl.state_dir(cfg) / "cache.json"
    cache = fl.read_json(cache_path, {}) or {}
    scan = mine(cfg, fl.parse_since(args.since), cache=cache, env=env,
                min_sessions=args.min_sessions)
    if not args.no_cache:
        fl.write_json(cache_path, cache)
    out = Path(args.out) if args.out else _run_dir(cfg) / "scan.json"
    fl.write_json(out, scan)

    print(f"scan.json -> {out}")
    src = scan["sources"]
    print(f"  sources: history={src['history']['count']} prompts, "
          f"transcripts={src['transcripts']['sessions']} sessions "
          f"({src['transcripts']['files_parsed']} parsed, "
          f"{src['transcripts']['cache_hits']} cached), plans={src['plans']['count']}")
    print(f"  existing automations: {scan['existing_automations']['skills']} skills, "
          f"{scan['existing_automations']['hooks']} hooks")
    print(f"  {scan['candidate_count']} candidates. Top {min(args.top, scan['candidate_count'])}:")
    for c in scan["candidates"][:args.top]:
        warn = "  [managed-hook warning]" if c.get("managed_hook_warning") else ""
        print(f"    [{c['score']:>7.2f}] {c['recommended_primitive']:<12} "
              f"{c['kind']:<18} {c['title'][:56]}{warn}")
    for w in env["warnings"]:
        print("  [warn]", w, file=sys.stderr)
    return 0


def cmd_validate_skill(args) -> int:
    return _print_skill(validate_skill(Path(args.path)), args.path)


def cmd_validate_hook(args) -> int:
    try:
        data = json.loads(Path(args.path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"cannot read/parse {args.path}: {e}", file=sys.stderr)
        return 1
    return _print_hook(validate_hook_obj(data), args.path)


def cmd_package(args) -> int:
    try:
        package_skill(Path(args.skill_dir), Path(args.out) if args.out else None,
                      strict=not args.no_strict)
    except (FileNotFoundError, ValueError) as e:
        print(f"package: {e}")
        return 1
    return 0


# --------------------------------------------------------------------------- #
def _safe_rel(rel: str) -> Path:
    """Reject absolute, drive/rooted, and parent-escaping artifact file keys.

    A drive-less *rooted* path like ``/etc/x`` or ``\\Windows\\x`` is NOT absolute
    on Windows (it has no drive) yet ``dest / that`` resolves to the drive root and
    escapes the skill dir, so we reject ``drive``/``root`` explicitly as well.
    """
    p = Path(rel)
    if p.is_absolute() or p.drive or p.root or ".." in p.parts:
        raise ValueError(f"unsafe artifact path: {rel}")
    return p


def _safe_project_base(project: str | None, cfg: Path, allowed_roots: list[Path]) -> Path:
    """Resolve the base dir for a project-scoped hookify rule, confined to an
    allow-list of roots (default: the user's home dir).

    ``project`` from a plan is attacker-influenceable (it derives from a mined
    transcript cwd, or an LLM/hand-authored plan), so an unconfined value could
    plant a runtime-read rule into any writable ``.claude`` dir = instruction
    injection into future Claude sessions. Returns ``<project>/.claude`` when the
    project resolves inside an allowed root, else raises ValueError. No project
    means the rule is config-dir scoped (returns ``cfg``).
    """
    if not project:
        return cfg
    pp = Path(project)
    if ".." in pp.parts:
        raise ValueError(f"unsafe project path (contains '..'): {project}")
    resolved = pp.expanduser().resolve()
    roots = [Path(r).expanduser().resolve() for r in allowed_roots]
    if not any(resolved == r or r in resolved.parents for r in roots):
        raise ValueError(
            f"project '{project}' resolves to {resolved}, outside allowed roots "
            f"{[str(r) for r in roots]}; pass --allow-project-root to permit it")
    return resolved / ".claude"


def _install_skill(cfg: Path, art: dict, do_write: bool) -> list[str]:
    name = fl.slugify(art.get("name", "unnamed-skill"))
    dest = cfg / "skills" / name
    actions = []
    files = art.get("files", {})
    if "SKILL.md" not in files:
        raise ValueError(f"skill '{name}' has no SKILL.md in files")
    for rel, content in files.items():
        target = dest / _safe_rel(rel)
        actions.append(f"skill: {target}")
        if do_write:
            fl.ensure_dir(target.parent)
            target.write_text(content, encoding="utf-8")
    if do_write:
        findings = validate_skill(dest)
        errs = [f["msg"] for f in findings if f["level"] == "error"]
        if errs:
            actions.append(f"  ! validation errors: {errs}")
    return actions


def _install_hookify(cfg: Path, art: dict, do_write: bool,
                     allowed_roots: list[Path]) -> list[str]:
    name = fl.slugify(art.get("name", "rule"))
    base = _safe_project_base(art.get("project"), cfg, allowed_roots)
    target = base / f"hookify.{name}.local.md"
    content = art.get("content", "")
    actions = [f"hookify-rule: {target}"]
    errs = [f["msg"] for f in validate_hookify_text(content) if f["level"] == "error"]
    if errs:
        actions.append(f"  ! rule validation errors: {errs}")
    if do_write:
        fl.ensure_dir(target.parent)
        target.write_text(content, encoding="utf-8")
    return actions


def _install_hook(cfg: Path, art: dict, env: dict, do_write: bool) -> list[str]:
    # Respect the managed-hooks policy: refuse to write an inert settings hook.
    if env.get("allow_managed_hooks_only"):
        return [f"hook: SKIPPED '{art.get('name')}' -- allowManagedHooksOnly is active; "
                f"emit a hookify-rule instead (settings hook would be inert)."]
    patch = art.get("settings_patch")
    findings = validate_hook_obj(patch or {})
    errs = [f["msg"] for f in findings if f["level"] == "error"]
    if errs:
        return [f"hook: INVALID '{art.get('name')}': {errs}"]
    target = cfg / "settings.json"
    # Surface exactly what will run so a human can vet it before approving
    # (mined command text is untrusted input).
    actions = [f"hook: {'merged into' if do_write else 'will merge into'} {target}"]
    for _event, entries in (patch.get("hooks", patch)).items():
        for entry in entries if isinstance(entries, list) else []:
            for h in (entry.get("hooks", []) if isinstance(entry, dict) else []):
                if isinstance(h, dict) and h.get("type") == "command" and h.get("command"):
                    actions.append(f"    will run: {h['command']}")
    if do_write:
        settings = fl.read_json(target, {}) or {}
        hooks = settings.setdefault("hooks", {})
        for event, entries in (patch.get("hooks", patch)).items():
            bucket = hooks.setdefault(event, [])
            for entry in entries:
                if entry not in bucket:  # idempotent: don't duplicate on re-run
                    bucket.append(entry)
        fl.write_json(target, settings)
    return actions


def cmd_install(args) -> int:
    cfg = fl.resolve_config_dir(args.config_dir)
    env = preflight(cfg)
    try:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"install: cannot read plan {args.plan}: {e}", file=sys.stderr)
        return 1

    do_write = bool(args.yes) and not args.dry_run
    mode = "WRITE" if do_write else "PREVIEW (no writes; pass --yes to apply)"
    print(f"install [{mode}] from {args.plan}")

    # Project-scoped hookify rules may only be written under these roots.
    allowed_roots = [Path.home()] + [Path(r) for r in (args.allow_project_root or [])]

    all_actions, installed = [], []
    for art in plan.get("artifacts", []):
        t = art.get("type")
        errored = False
        try:
            if t == "skill":
                acts = _install_skill(cfg, art, do_write)
            elif t == "hookify-rule":
                acts = _install_hookify(cfg, art, do_write, allowed_roots)
            elif t == "hook":
                acts = _install_hook(cfg, art, env, do_write)
            else:
                acts = [f"unknown artifact type: {t}"]
        except ValueError as e:
            acts = [f"ERROR ({art.get('name')}): {e}"]
            errored = True
        all_actions.extend(acts)
        # Only record a genuinely-written artifact in the manifest (a refused
        # install must not corrupt incremental-rerun dedup).
        if do_write and t and not errored:
            installed.append({"type": t, "name": art.get("name"),
                              "candidate": art.get("candidate_id")})

    for a in all_actions:
        print("  " + a)

    if do_write and installed:
        manifest_path = fl.state_dir(cfg) / "manifest.json"
        manifest = fl.read_json(manifest_path, {"installs": []}) or {"installs": []}
        manifest["installs"].append({
            "at": datetime.now(timezone.utc).isoformat(),
            "artifacts": installed,
        })
        fl.write_json(manifest_path, manifest)
        print(f"  manifest updated: {manifest_path}")
    return 0


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="groundhog", description="groundhog orchestrator.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_cfg(p):
        p.add_argument("--config-dir", help="Override ~/.claude / $CLAUDE_CONFIG_DIR.")

    p = sub.add_parser("preflight"); add_cfg(p); p.set_defaults(func=cmd_preflight)

    p = sub.add_parser("scan"); add_cfg(p)
    p.add_argument("--since", default=fl.RECENCY_DEFAULT, help="Window, e.g. 30d / 2w / all.")
    p.add_argument("--out", help="scan.json output path.")
    p.add_argument("--min-sessions", type=int, default=3)
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--no-cache", action="store_true")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("validate-skill"); p.add_argument("path"); p.set_defaults(func=cmd_validate_skill)
    p = sub.add_parser("validate-hook"); p.add_argument("path"); add_cfg(p); p.set_defaults(func=cmd_validate_hook)

    p = sub.add_parser("package"); p.add_argument("skill_dir")
    p.add_argument("--out"); p.add_argument("--no-strict", action="store_true")
    p.set_defaults(func=cmd_package)

    p = sub.add_parser("install"); add_cfg(p)
    p.add_argument("--plan", required=True, help="Path to an install-plan JSON.")
    p.add_argument("--yes", action="store_true", help="Actually write (default: preview).")
    p.add_argument("--dry-run", action="store_true", help="Force preview even with --yes.")
    p.add_argument("--allow-project-root", action="append", default=[],
                   help="Permit project-scoped hookify rules under this root "
                        "(repeatable). Default allowed root: your home dir.")
    p.set_defaults(func=cmd_install)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
