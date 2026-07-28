#!/usr/bin/env python3
"""
package_skill.py -- bundle a skill directory into a distributable .zip.

Validates the skill first (frontmatter must pass), then zips the directory with
the skill folder as the archive root, skipping junk (``__pycache__``, ``*.pyc``,
VCS metadata). Original implementation, pure stdlib.

    python scripts/package_skill.py path/to/skill-dir [--out dist/skill.zip]
"""
from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

from validate_skill import validate_skill

_SKIP_DIRS = {"__pycache__", ".git", ".mypy_cache", ".pytest_cache", "node_modules"}
_SKIP_SUFFIX = {".pyc", ".pyo", ".tmp"}
_SKIP_NAMES = {".DS_Store"}


def _included(p: Path, root: Path) -> bool:
    # Never bundle a symlink, or anything whose real path escapes the skill dir —
    # a symlink could otherwise pull external content into the published zip.
    if p.is_symlink():
        return False
    try:
        p.resolve().relative_to(root)
    except ValueError:
        return False
    if any(part in _SKIP_DIRS for part in p.parts):
        return False
    if p.suffix in _SKIP_SUFFIX or p.name in _SKIP_NAMES:
        return False
    return True


def package_skill(skill_dir: Path, out: Path | None = None, strict: bool = True) -> Path:
    skill_dir = skill_dir.resolve()
    if not (skill_dir / "SKILL.md").exists():
        raise FileNotFoundError(f"No SKILL.md in {skill_dir}")

    findings = validate_skill(skill_dir)
    errors = [f for f in findings if f["level"] == "error"]
    if errors and strict:
        raise ValueError("Skill failed validation: "
                         + "; ".join(f["msg"] for f in errors))

    out = Path(out) if out else skill_dir.parent / f"{skill_dir.name}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)

    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(skill_dir.rglob("*")):
            if p.is_file() and _included(p, skill_dir):
                arc = Path(skill_dir.name) / p.relative_to(skill_dir)
                zf.write(p, arcname=str(arc))
                count += 1
    print(f"packaged {count} file(s) -> {out}")
    return out


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Package a skill directory into a zip.")
    ap.add_argument("skill_dir")
    ap.add_argument("--out")
    ap.add_argument("--no-strict", action="store_true",
                    help="Package even if validation reports errors.")
    args = ap.parse_args(argv)
    try:
        package_skill(Path(args.skill_dir), Path(args.out) if args.out else None,
                      strict=not args.no_strict)
    except (FileNotFoundError, ValueError) as e:
        print(f"package_skill: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
