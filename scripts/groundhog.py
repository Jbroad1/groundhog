#!/usr/bin/env python3
"""
groundhog.py -- groundhog CLI orchestrator (the deterministic backbone).

Sub-commands:

  preflight            Detect env + managed-hooks policy + optional skills.
  scan                 Preflight + mine ~/.claude into a scored scan.json.
  shard                Split the leverage-ranked shortlist into N parallel shards.
  merge                Fold worker verdict shards into ranked finalists.
  remember             Fold a verdict.json into the durable verdict ledger.
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
from index_store import IndexStore
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
    since = fl.parse_since(args.since)

    # Durable SQLite index by default (incremental, crash-safe, remembers prior
    # verdicts). `--no-cache` runs a throwaway in-memory scan with no persistence.
    store = None if args.no_cache else IndexStore.open(cfg)
    try:
        scan = mine(cfg, since, cache=({} if store is None else None), env=env,
                    min_sessions=args.min_sessions, store=store)
        if store is not None:
            # Watchman-style commit: record success only after the mine completed.
            store.set_meta("last_run_epoch", fl.now_ms())
    finally:
        if store is not None:
            store.close()

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


# --------------------------------------------------------------------------- #
# Map-reduce helpers: `shard` splits the leverage-ranked shortlist across N
# parallel analyzer workers; `merge` folds their partial verdicts back into one
# ranked finalist list. Both are deterministic (no LLM) so they are unit-testable
# and byte-reproducible -- the LLM judgment lives only in the workers between them.
# --------------------------------------------------------------------------- #
def _load_scan(path: str) -> dict:
    data = fl.read_json(Path(path))
    if not isinstance(data, dict) or "candidates" not in data:
        raise ValueError(f"not a scan.json (no 'candidates'): {path}")
    return data


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _merge_proofs(group, cap: int = 5) -> list:
    """Union the proof paths across a cluster, de-duplicated and capped, in a
    deterministic order (first shard read first)."""
    seen, keys = [], set()
    for p in group:
        for pp in (p.get("proof_paths") or []):
            if not isinstance(pp, dict):
                continue
            k = (pp.get("file"), pp.get("line"), pp.get("detail"))
            if k in keys:
                continue
            keys.add(k)
            seen.append(pp)
            if len(seen) >= cap:
                return seen
    return seen


def cmd_shard(args) -> int:
    scan = _load_scan(args.scan)
    cands = scan.get("candidates", [])
    top = cands[:args.top] if args.top and args.top > 0 else cands
    n = max(1, args.n)
    out_dir = Path(args.out_dir) if args.out_dir else Path(args.scan).resolve().parent / "shards"
    fl.ensure_dir(out_dir)

    # Striped split: shard i gets candidates i, i+n, i+2n, ... The scan is already
    # leverage-ranked, so striping gives every worker a representative
    # cross-section of leverage -- NOT a contiguous high-to-low block (which would
    # make cross-shard scores incomparable). Stable by cand-NNNN id / position.
    common = {
        "schema_version": scan.get("schema_version", fl.SCHEMA_VERSION),
        "shard_count": n,
        "config_dir": scan.get("config_dir", ""),
        "env": scan.get("env", {}),
        "existing_automations": scan.get("existing_automations", {}),
    }
    written = []
    for i in range(n):
        chunk = top[i::n]
        if not chunk:
            continue
        shard = dict(common)
        shard["shard_index"] = i
        shard["candidate_count"] = len(chunk)
        shard["candidates"] = chunk
        p = out_dir / f"shard-{i:02d}.json"
        fl.write_json(p, shard)
        written.append(p)

    print(f"shard: top {len(top)} of {len(cands)} candidates -> {len(written)} shard(s) "
          f"of ~{len(top)//n or len(top)} each in {out_dir}")
    for p in written:
        print(f"  {p}")
    print("  next: dispatch one analyzer subagent per shard (in a single message, "
          "in parallel); each writes verdict-shard-NN.json next to its shard.")
    return 0


def cmd_merge(args) -> int:
    shards_dir = Path(args.shards)
    verdict_files = sorted(shards_dir.glob("verdict-shard-*.json"))
    if not verdict_files:
        print(f"merge: no verdict-shard-*.json in {shards_dir} "
              f"(did the analyzer workers run?)", file=sys.stderr)
        return 1

    proposals, shard_count, timed_out = [], 0, []
    for vf in verdict_files:
        data = fl.read_json(vf, {}) or {}
        shard_count += 1
        for p in data.get("proposals", []):
            if isinstance(p, dict):
                proposals.append(p)
        if data.get("timed_out"):
            timed_out.append(data.get("shard_index"))

    # Cluster cross-shard near-duplicates on dedup_key (a workflow split across two
    # shards must collapse to one finalist). Fall back to candidate_id / title when
    # a worker omitted the key, so a keyless proposal still survives as its own row.
    clusters: dict = {}
    for p in proposals:
        key = str(p.get("dedup_key") or p.get("candidate_id")
                  or fl.slugify(str(p.get("title", "")))).lower()
        clusters.setdefault(key, []).append(p)

    finalists = []
    for key, group in clusters.items():
        # Strongest proposal in the cluster represents it; fully-ordered key makes
        # the pick deterministic regardless of shard read order.
        rep = dict(sorted(group, key=lambda p: (
            -_num(p.get("score")), -_num(p.get("confidence")),
            -len(p.get("proof_paths") or []), str(p.get("candidate_id", ""))))[0])
        rep["dedup_key"] = key
        rep["proof_paths"] = _merge_proofs(group)
        rep["cluster_candidate_ids"] = sorted(
            {str(p.get("candidate_id", "")) for p in group if p.get("candidate_id")})
        finalists.append(rep)

    finalists.sort(key=lambda p: (-_num(p.get("score")), -_num(p.get("confidence")),
                                  str(p.get("dedup_key", ""))))
    limit = args.limit if args.limit and args.limit > 0 else 18
    finalists = finalists[:limit]

    out = Path(args.out) if args.out else shards_dir.parent / "finalists.json"
    fl.write_json(out, {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "shard_count": shard_count,
        "timed_out_shards": [t for t in timed_out if t is not None],
        "proposal_count": len(proposals),
        "finalist_count": len(finalists),
        "finalists": finalists,
    })
    print(f"merge: {len(proposals)} proposals from {shard_count} shard(s) -> "
          f"{len(finalists)} finalists -> {out}")
    if timed_out:
        print(f"  [warn] shard(s) timed out; merged on partial results: {timed_out}",
              file=sys.stderr)
    print("  next: re-score these finalists in one pass (main context) for global "
          "calibration, cap to the top 5-8, and write verdict.json.")
    return 0


def _coerce_conf(v):
    """Confidence from an untrusted verdict.json may be a string/garbage; never let
    it crash the ledger write."""
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _resolve_sig(entry: dict, cand_map: dict):
    """Resolve a verdict entry to a ledger key + evidence fingerprint. Prefer the
    entry's OWN signature (stable across runs, so `remember` works without --scan);
    fall back to the scan.json map, then to a weaker key. Returns
    (sig_or_None, fp_or_None, strong) where strong=False means the key won't match
    a future signature-keyed lookup."""
    sig = entry.get("signature")
    fp = entry.get("evidence_fingerprint")
    if sig:
        return sig, fp, True
    msig, mfp = cand_map.get(entry.get("candidate_id"), (None, None))
    if msig:
        return msig, (fp or mfp), True
    return (entry.get("dedup_key") or entry.get("candidate_id")), (fp or mfp), False


def cmd_remember(args) -> int:
    """Fold a verdict.json's decisions into the durable verdict ledger. This is a
    MERGE (one upsert per signature), never a blind overwrite of the ledger, so a
    later run can surface what was decided before instead of re-asking."""
    cfg = fl.resolve_config_dir(args.config_dir)
    verdict = fl.read_json(Path(args.verdict))
    if not isinstance(verdict, dict):
        print(f"remember: cannot read verdict {args.verdict}", file=sys.stderr)
        return 1

    # Map candidate_id -> (signature, evidence_fingerprint) from scan.json as a
    # FALLBACK for proposals that don't carry their own signature.
    cand_map = {}
    if args.scan:
        scan = fl.read_json(Path(args.scan), {}) or {}
        for c in scan.get("candidates", []):
            cand_map[c.get("id")] = (c.get("signature"), c.get("evidence_fingerprint"))

    store = IndexStore.open(cfg)
    kept = dropped = weak = 0
    try:
        for p in verdict.get("proposals", []):
            if not isinstance(p, dict):
                continue  # untrusted input: skip non-object entries
            sig, fp, strong = _resolve_sig(p, cand_map)
            if not sig:
                continue
            weak += 0 if strong else 1
            store.upsert_verdict(
                sig, p.get("primitive"), "keep",
                p.get("rationale") or p.get("justification") or p.get("plain_summary"),
                _coerce_conf(p.get("confidence")), fp)
            kept += 1
        for d in verdict.get("dropped", []):
            if not isinstance(d, dict):
                continue
            sig, fp, strong = _resolve_sig(d, cand_map)
            if not sig:
                continue
            weak += 0 if strong else 1
            store.upsert_verdict(sig, None, "drop", d.get("reason"), None, fp)
            dropped += 1
        store.set_meta("last_remember_epoch", fl.now_ms())
    finally:
        store.close()

    print(f"remember: folded {kept} kept + {dropped} dropped decision(s) into the "
          f"verdict ledger ({fl.state_dir(cfg) / 'index.db'}).")
    if weak:
        print(f"  [warn] {weak} decision(s) had no signature and no scan.json match, so "
              f"they were keyed on a fallback a future scan's signature lookup won't hit. "
              f"Pass --scan <the scan.json that produced this verdict>, or have the "
              f"analyzer emit `signature` per proposal.", file=sys.stderr)
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

    Keys are mined or LLM-authored and untrusted, and may use POSIX ('/') or
    Windows ('\\') separators. Both are treated as separators on every OS: on
    POSIX, ``Path('..\\evil')`` is a single filename, so relying on host Path
    semantics would let a Windows-style traversal slip through. A drive-less
    *rooted* path ('/etc/x', '\\Windows\\x') is not "absolute" on Windows yet still
    escapes to the root, so a leading separator and a drive letter are rejected
    explicitly too.
    """
    norm = rel.replace("\\", "/")
    if (norm.startswith("/")                        # rooted (either separator)
            or ".." in norm.split("/")              # parent escape
            or (len(norm) >= 2 and norm[1] == ":")  # drive letter, e.g. C:/x
            or Path(rel).is_absolute()):            # any host-absolute form
        raise ValueError(f"unsafe artifact path: {rel}")
    return Path(norm)


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

    p = sub.add_parser("shard", help="Split the leverage-ranked shortlist into N parallel worker shards.")
    p.add_argument("--scan", required=True, help="Path to scan.json.")
    p.add_argument("--n", type=int, default=5, help="Number of shards / parallel workers (default 5).")
    p.add_argument("--top", type=int, default=500, help="Take the top-N leverage-ranked candidates (default 500).")
    p.add_argument("--out-dir", help="Where to write shard-NN.json (default: <scan_dir>/shards).")
    p.set_defaults(func=cmd_shard)

    p = sub.add_parser("merge", help="Fold worker verdict-shard-NN.json into ranked finalists.")
    p.add_argument("--shards", required=True, help="Dir holding verdict-shard-*.json.")
    p.add_argument("--scan", help="Path to scan.json (for context; optional).")
    p.add_argument("--limit", type=int, default=18, help="Max finalists to emit (default 18).")
    p.add_argument("--out", help="finalists.json output path (default: <shards_dir>/../finalists.json).")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("remember", help="Fold a verdict.json's decisions into the durable verdict ledger.")
    add_cfg(p)
    p.add_argument("--verdict", required=True, help="Path to the final verdict.json.")
    p.add_argument("--scan", help="scan.json, to key the ledger on signature (recommended).")
    p.set_defaults(func=cmd_remember)

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
