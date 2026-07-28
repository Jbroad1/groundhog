#!/usr/bin/env python3
"""
GREEN gate: the RED-baseline scenario now succeeds end-to-end WITH groundhog.

Proves the full loop on hermetic fixtures: scan -> pick a proof-backed candidate
-> author a skill from it -> install (gated) -> the emitted artifact validates
and is recorded in the manifest. This is the writing-skills GREEN counterpart to
references/red-baseline.md.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import groundhog                       # noqa: E402
from mine_workflows import mine    # noqa: E402
from validate_skill import validate_skill  # noqa: E402
from make_fixtures import build    # noqa: E402


class GreenGate(unittest.TestCase):
    def test_scenario_succeeds_end_to_end(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = build(Path(tmp.name) / "cfg")

        # 1) SCAN — mine finds a proof-backed, repeated workflow.
        scan = mine(cfg, since_ms=None, cache={})
        cand = next(c for c in scan["candidates"]
                    if c["signature"] == ["Grep", "Read", "Edit"])
        self.assertTrue(cand["proof_paths"] and Path(cand["proof_paths"][0]["file"]).exists())

        # 2) COMPILE — author a skill from the candidate (what phase 4 produces).
        name = "grep-read-edit-flow"
        skill_md = (f"---\nname: {name}\n"
                    "description: Use when you repeatedly grep, then read, then edit the "
                    "same area of a repo.\n---\n\n"
                    f"# {name}\n\n## Steps\n1. Grep for the symbol.\n2. Read the file.\n"
                    "3. Make the edit.\n")
        plan = {"artifacts": [{"type": "skill", "name": name,
                               "candidate_id": cand["id"],
                               "files": {"SKILL.md": skill_md}}]}
        plan_path = Path(tmp.name) / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")

        # 3) INSTALL — preview writes nothing; --yes applies.
        buf = io.StringIO()
        with redirect_stdout(buf):
            groundhog.main(["install", "--config-dir", str(cfg), "--plan", str(plan_path)])
        self.assertFalse((cfg / "skills" / name).exists(), "preview must not write")

        with redirect_stdout(io.StringIO()):
            groundhog.main(["install", "--config-dir", str(cfg), "--plan", str(plan_path), "--yes"])

        # 4) QA — the emitted skill exists and validates cleanly.
        emitted = cfg / "skills" / name
        self.assertTrue((emitted / "SKILL.md").exists())
        errs = [f for f in validate_skill(emitted) if f["level"] == "error"]
        self.assertEqual(errs, [], f"emitted skill failed validation: {errs}")

        # 5) manifest records the install for incremental re-runs.
        manifest = json.loads((cfg / "groundhog" / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(any(a["name"] == name
                            for inst in manifest["installs"] for a in inst["artifacts"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
