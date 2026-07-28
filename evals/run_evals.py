#!/usr/bin/env python3
"""
run_evals.py -- execute groundhog's deterministic (code-graded) evals and print
a pass/fail table. This makes evals.json *enforced*, not just documentation.

Model-graded evals (trigger discovery t*, proposal quality a10) require a judge
with the same context the system sees and are run at iteration time, not here --
they are intentionally not executed by this runner (see evals/evals.json).

Pure stdlib, cross-platform (no shell pipelines). CI-safe.

    python evals/run_evals.py            # run the deterministic gate
    python evals/run_evals.py --json     # machine-readable summary
"""
from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Deterministic application evals: (id, human label, unittest target(s)).
# Mirrors the `in_runner` evals in evals.json. Exit 0 from unittest == pass.
CODE_EVALS = [
    ("a1", "scan-determinism",   "test_engine"),
    ("a2", "emitted-skill-valid", "test_tools.ValidateSkillTests"),
    ("a3", "emitted-hook-valid",  "test_tools.ValidateHookTests test_tools.ValidateHookifyTests"),
    ("a4", "no-secrets",          "test_engine.EngineTests.test_secret_scrubbed_everywhere test_engine.ScrubTests"),
    ("a5", "managed-policy",      "test_engine.EngineTests.test_bash_guardrail_and_managed_policy test_cli.CliTests.test_install_yes_writes_and_respects_managed_policy"),
    ("a6", "review-by-default",   "test_cli.CliTests.test_install_preview_writes_nothing test_cli.CliTests.test_dry_run_overrides_yes"),
    ("a7", "path-confinement",    "test_cli.PathConfinementTests"),
]

# a8: portable "no third-party imports" check (replaces evals.json's grep, which
# is not cross-platform). Modules groundhog is allowed to import.
_STDLIB_OK = {
    "argparse", "ast", "json", "re", "os", "sys", "hashlib", "pathlib",
    "datetime", "platform", "shutil", "zipfile", "collections", "subprocess",
    "itertools", "functools", "typing", "__future__", "io", "contextlib",
    "tempfile", "unittest",
}
_LOCAL_PREFIXES = ("groundhog", "scan_", "mine_", "scrub", "validate_", "package_", "preflight")


def _run_unittest(targets: str) -> bool:
    r = subprocess.run(
        [sys.executable, "-m", "unittest", *targets.split()],
        cwd=str(ROOT / "tests"), capture_output=True, text=True,
    )
    return r.returncode == 0


def _standalone_imports_ok() -> tuple[bool, list[str]]:
    bad: list[str] = []
    for py in sorted((ROOT / "scripts").glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module.split(".")[0]]
            for m in mods:
                if m in _STDLIB_OK or m.startswith(_LOCAL_PREFIXES):
                    continue
                bad.append(f"{py.name}: {m}")
    return (not bad), bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run groundhog deterministic evals.")
    ap.add_argument("--json", action="store_true", help="Machine-readable summary.")
    args = ap.parse_args(argv)

    results = []  # (id, label, ok)
    for eid, label, targets in CODE_EVALS:
        results.append((eid, label, _run_unittest(targets)))
    imp_ok, bad = _standalone_imports_ok()
    results.append(("a8", "standalone-imports", imp_ok))

    passed = sum(1 for *_, ok in results if ok)
    total = len(results)

    if args.json:
        print(json.dumps({
            "passed": passed, "total": total,
            "results": [{"id": i, "check": l, "pass": o} for i, l, o in results],
            "third_party_imports": bad,
        }, indent=2))
    else:
        for i, l, o in results:
            print(f"  [{'PASS' if o else 'FAIL'}] {i:4} {l}")
        if bad:
            print("  third-party imports found:", ", ".join(bad))
        print(f"\n  {passed}/{total} deterministic evals passed.")
        print("  (trigger discovery + proposal quality are model-graded - run at "
              "iteration time with a judge; see evals/evals.json.)")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
