#!/usr/bin/env python3
"""End-to-end CLI tests for groundhog.py against the fixture config dir."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import groundhog  # noqa: E402
from make_fixtures import build  # noqa: E402

PLAN = {
    "artifacts": [
        {"type": "skill", "name": "demo-skill", "candidate_id": "cand-0001",
         "files": {"SKILL.md": "---\nname: demo-skill\n"
                   "description: Use when you want the demo.\n---\n\n# Demo\nHi.\n"}},
        {"type": "hookify-rule", "name": "guard", "content": "# guard rule\n"},
        {"type": "hook", "name": "blocked-hook",
         "settings_patch": {"hooks": {"PostToolUse": [
             {"matcher": "Edit", "hooks": [{"type": "command", "command": "echo hi"}]}]}}},
    ]
}


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = build(Path(self._tmp.name) / "cfg")
        self.plan_path = Path(self._tmp.name) / "plan.json"
        self.plan_path.write_text(json.dumps(PLAN), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = groundhog.main(argv)
        return rc, buf.getvalue()

    def test_scan_writes_scanjson(self):
        out = Path(self._tmp.name) / "scan.json"
        rc, log = self._run(["scan", "--config-dir", str(self.cfg), "--since", "all",
                             "--out", str(out)])
        self.assertEqual(rc, 0)
        self.assertTrue(out.exists())
        scan = json.loads(out.read_text(encoding="utf-8"))
        self.assertGreater(scan["candidate_count"], 0)
        self.assertIn("Grep -> Read -> Edit",
                      [c["title"] for c in scan["candidates"]])

    def test_install_preview_writes_nothing(self):
        rc, log = self._run(["install", "--config-dir", str(self.cfg),
                             "--plan", str(self.plan_path)])
        self.assertEqual(rc, 0)
        self.assertIn("PREVIEW", log)
        self.assertFalse((self.cfg / "skills" / "demo-skill").exists(),
                         "preview must not create the skill")

    def test_install_yes_writes_and_respects_managed_policy(self):
        rc, log = self._run(["install", "--config-dir", str(self.cfg),
                             "--plan", str(self.plan_path), "--yes"])
        self.assertEqual(rc, 0)
        # skill written + valid
        self.assertTrue((self.cfg / "skills" / "demo-skill" / "SKILL.md").exists())
        # hookify rule written
        self.assertTrue((self.cfg / "hookify.guard.local.md").exists())
        # hook refused because the fixture sets allowManagedHooksOnly
        self.assertIn("SKIPPED", log)
        self.assertIn("allowManagedHooksOnly", log)
        # manifest recorded
        self.assertTrue((self.cfg / "groundhog" / "manifest.json").exists())

    def test_dry_run_overrides_yes(self):
        rc, log = self._run(["install", "--config-dir", str(self.cfg),
                             "--plan", str(self.plan_path), "--yes", "--dry-run"])
        self.assertIn("PREVIEW", log)
        self.assertFalse((self.cfg / "skills" / "demo-skill").exists())

    def test_hook_install_is_idempotent(self):
        # Re-running install with the same plan must not append duplicate hook
        # entries (which would make the hook fire twice and corrupt settings).
        box = Path(tempfile.mkdtemp())
        cfg = box / "cfg"; cfg.mkdir()
        art = {"type": "hook", "name": "h",
               "settings_patch": {"hooks": {"PostToolUse": [
                   {"matcher": "Edit", "hooks": [{"type": "command", "command": "echo hi"}]}]}}}
        groundhog._install_hook(cfg, art, {}, True)
        groundhog._install_hook(cfg, art, {}, True)
        settings = json.loads((cfg / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(len(settings["hooks"]["PostToolUse"]), 1,
                         "identical hook entry was appended twice")

    def test_hook_preview_surfaces_command(self):
        # Preview must show the exact command that would run, and write nothing.
        box = Path(tempfile.mkdtemp())
        cfg = box / "cfg"; cfg.mkdir()
        art = {"type": "hook", "name": "h",
               "settings_patch": {"hooks": {"PostToolUse": [
                   {"matcher": "Edit", "hooks": [{"type": "command", "command": "echo hi"}]}]}}}
        acts = groundhog._install_hook(cfg, art, {}, False)  # preview (do_write=False)
        self.assertTrue(any("echo hi" in a for a in acts),
                        "preview did not surface the command that would run")
        self.assertFalse((cfg / "settings.json").exists(), "preview wrote settings.json")


class PathConfinementTests(unittest.TestCase):
    def test_safe_rel_rejects_traversal_and_rooted(self):
        # Rooted-but-drive-less paths ("/etc/x", "\\Windows\\x") are NOT absolute
        # on Windows yet escape to the drive root -- must be rejected.
        for bad in ["../evil", "..\\evil", "/etc/passwd",
                    "\\Windows\\System32\\x", "sub/../../x"]:
            with self.assertRaises(ValueError, msg=bad):
                groundhog._safe_rel(bad)

    def test_safe_rel_allows_normal_relative(self):
        self.assertEqual(str(groundhog._safe_rel("scripts/tool.py")).replace("\\", "/"),
                         "scripts/tool.py")

    def test_safe_project_base_confines_to_allowed_roots(self):
        box = Path(tempfile.mkdtemp())
        allowed = box / "allowed"; allowed.mkdir()
        outside = box / "outside"; outside.mkdir()
        cfg = box / "cfg"
        # inside an allowed root -> returns <project>/.claude
        self.assertEqual(
            groundhog._safe_project_base(str(allowed / "proj"), cfg, [allowed]),
            (allowed / "proj").resolve() / ".claude")
        # outside every allowed root -> refused
        with self.assertRaises(ValueError):
            groundhog._safe_project_base(str(outside / "proj"), cfg, [allowed])
        # traversal -> refused
        with self.assertRaises(ValueError):
            groundhog._safe_project_base("../../etc", cfg, [allowed])
        # no project -> falls back to the config dir
        self.assertEqual(groundhog._safe_project_base(None, cfg, [allowed]), cfg)

    def test_install_refuses_hookify_project_escape(self):
        # Fake home so the test is independent of where tempfile lives (on Windows
        # tempdirs sit under the real home, which is a default allowed root).
        box = Path(tempfile.mkdtemp())
        home = box / "home"; home.mkdir()
        outside = box / "outside"; outside.mkdir()   # sibling of home, not under it
        cfg = build(box / "cfg")
        plan = {"artifacts": [{"type": "hookify-rule", "name": "pwned",
                               "project": str(outside), "content": "# x\n"}]}
        pf = box / "plan.json"
        pf.write_text(json.dumps(plan), encoding="utf-8")
        buf = io.StringIO()
        with mock.patch.object(groundhog.Path, "home", return_value=home), redirect_stdout(buf):
            groundhog.main(["install", "--config-dir", str(cfg), "--plan", str(pf), "--yes"])
        log = buf.getvalue()
        self.assertFalse((outside / ".claude" / "hookify.pwned.local.md").exists(),
                         "hookify rule escaped to a disallowed project dir")
        self.assertIn("ERROR", log)

    def test_manifest_excludes_refused_install(self):
        # A refused artifact (project escape) must NOT be logged in manifest.json
        # as installed, or incremental-rerun dedup is corrupted.
        box = Path(tempfile.mkdtemp())
        home = box / "home"; home.mkdir()
        outside = box / "outside"; outside.mkdir()
        cfg = build(box / "cfg")
        plan = {"artifacts": [
            {"type": "skill", "name": "ok-skill",
             "files": {"SKILL.md": "---\nname: ok-skill\n"
                       "description: Use when you want ok.\n---\n\n# Ok\nHi.\n"}},
            {"type": "hookify-rule", "name": "refused",
             "project": str(outside), "content": "# x\n"},
        ]}
        pf = box / "plan.json"
        pf.write_text(json.dumps(plan), encoding="utf-8")
        buf = io.StringIO()
        with mock.patch.object(groundhog.Path, "home", return_value=home), redirect_stdout(buf):
            groundhog.main(["install", "--config-dir", str(cfg), "--plan", str(pf), "--yes"])
        manifest = json.loads((cfg / "groundhog" / "manifest.json").read_text(encoding="utf-8"))
        names = [a["name"] for inst in manifest["installs"] for a in inst["artifacts"]]
        self.assertIn("ok-skill", names)
        self.assertNotIn("refused", names, "manifest logged a refused install")

    def test_install_allows_hookify_project_under_allowed_root(self):
        # --allow-project-root opts a project location in; the rule must still write.
        box = Path(tempfile.mkdtemp())
        home = box / "home"; home.mkdir()
        allowed = box / "allowed"; allowed.mkdir()
        proj = allowed / "proj"; proj.mkdir()
        cfg = build(box / "cfg")
        plan = {"artifacts": [{"type": "hookify-rule", "name": "okrule",
                               "project": str(proj), "content": "# ok\n"}]}
        pf = box / "plan.json"
        pf.write_text(json.dumps(plan), encoding="utf-8")
        buf = io.StringIO()
        with mock.patch.object(groundhog.Path, "home", return_value=home), redirect_stdout(buf):
            groundhog.main(["install", "--config-dir", str(cfg), "--plan", str(pf),
                        "--yes", "--allow-project-root", str(allowed)])
        self.assertTrue((proj / ".claude" / "hookify.okrule.local.md").exists(),
                        "allowed project-scoped rule was not written")


if __name__ == "__main__":
    unittest.main(verbosity=2)
