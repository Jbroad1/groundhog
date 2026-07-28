#!/usr/bin/env python3
"""
Unit tests for the individual scanners, which previously had only end-to-end
coverage through test_engine. Here we exercise their edge cases directly:

  * scan_history  -- prompt normalization, missing file, missing `display` key,
                     malformed/binary JSONL, out-of-window timestamp filter, and
                     secret scrubbing of the retained sample.
  * scan_plans    -- title/heading/step parsing, recurring-shape norm key,
                     missing dir, mtime-based since-filter, heading scrubbing.
  * preflight     -- managed-hooks policy precedence across the settings files,
                     optional-skill detection.
  * scan_transcripts -- the per-file MAX_LINES cap stops a mega-transcript early.

Pure stdlib `unittest` (no pytest), matching test_engine.py::

    python -m unittest discover -s tests -v
    # or
    python tests/test_scanners.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import scan_transcripts                                    # noqa: E402
from scan_history import scan_history, _norm_prompt        # noqa: E402
from scan_plans import scan_plans, _parse_plan             # noqa: E402
from preflight import preflight, _managed_hooks_policy, _find_skill  # noqa: E402

TOKEN = "ghp_EXAMPLEONLYNOTAREALTOKEN00"  # obviously-synthetic GitHub-token shape


def _tmp(case) -> Path:
    """A self-cleaning temp dir bound to the test's lifetime."""
    td = tempfile.TemporaryDirectory()
    case.addCleanup(td.cleanup)
    return Path(td.name)


class ScanHistoryTests(unittest.TestCase):
    def _write(self, cfg: Path, raw: bytes) -> None:
        (cfg / "history.jsonl").write_bytes(raw)

    def test_missing_file(self):
        res = scan_history(_tmp(self), None)
        self.assertFalse(res["exists"])
        self.assertEqual(res["count"], 0)
        self.assertEqual(res["total"], 0)
        self.assertEqual(res["prompts"], [])

    def test_norm_prompt_masks_volatile_operands(self):
        # template_bash masks quoted strings / numbers / paths, then we lowercase:
        # two prompts differing only in operands must collapse to one cluster key.
        a = _norm_prompt('Please DEPLOY build 12345 to "prod-east"')
        b = _norm_prompt('Please DEPLOY build 67890 to "prod-west"')
        self.assertEqual(a, b)
        self.assertEqual(a, a.lower())

    def test_missing_display_key_skipped(self):
        cfg = _tmp(self)
        self._write(cfg, (
            b'{"display": "fix the auth bug", "timestamp": 1753600000000}\n'
            b'{"no_display": true, "timestamp": 1753600000000}\n'
        ))
        res = scan_history(cfg, None)
        # a record without a `display` key is skipped BEFORE it is counted.
        self.assertEqual(res["total"], 1)
        self.assertEqual(res["count"], 1)
        self.assertFalse(res["prompts"][0]["is_slash"])

    def test_malformed_and_binary_lines_skipped(self):
        cfg = _tmp(self)
        self._write(cfg, (
            b'{"display": "valid one", "timestamp": 1753600000000}\n'
            b'{ this is not valid json at all\n'
            b'\xff\xfe\x80 raw binary garbage \x00\n'
            b'\n'
            b'{"display": "/deploy", "timestamp": 1753600000000}\n'
        ))
        res = scan_history(cfg, None)
        # only the two well-formed dict-with-display lines survive; the malformed,
        # binary, and blank lines are dropped silently by iter_jsonl.
        self.assertEqual(res["total"], 2)
        self.assertEqual(res["count"], 2)
        slash = [p for p in res["prompts"] if p["is_slash"]]
        self.assertEqual(len(slash), 1)
        self.assertEqual(slash[0]["slash"], "/deploy")

    def test_out_of_window_timestamp_filtered(self):
        cfg = _tmp(self)
        self._write(cfg, (
            b'{"display": "old prompt", "timestamp": 1000000000000}\n'
            b'{"display": "new prompt", "timestamp": 2000000000000}\n'
        ))
        res = scan_history(cfg, since_ms=1500000000000)
        # both dict-with-display records are counted; only the in-window one is kept.
        self.assertEqual(res["total"], 2)
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["prompts"][0]["sample"], "new prompt")

    def test_secret_scrubbed_from_sample(self):
        cfg = _tmp(self)
        self._write(cfg,
                    ('{"display": "deploy with token=%s", "timestamp": 1753600000000}\n'
                     % TOKEN).encode("utf-8"))
        res = scan_history(cfg, None)
        self.assertEqual(res["count"], 1)
        self.assertNotIn(TOKEN, json.dumps(res), "token leaked into history sample")


class ScanPlansTests(unittest.TestCase):
    _PLAN = (
        "# Refactor the auth module\n\n"
        "## Context\nThe auth module is messy.\n\n"
        "## Approach\n1. Extract helpers\n2. Add tests\n3. Wire it up\n"
    )

    def _plans_dir(self) -> Path:
        cfg = _tmp(self)
        (cfg / "plans").mkdir()
        return cfg

    def test_missing_dir(self):
        res = scan_plans(_tmp(self), None)
        self.assertFalse(res["exists"])
        self.assertEqual(res["count"], 0)
        self.assertEqual(res["plans"], [])

    def test_parse_plan_fields(self):
        cfg = self._plans_dir()
        p = cfg / "plans" / "plan-001.md"
        p.write_text(self._PLAN, encoding="utf-8")
        parsed = _parse_plan(p)
        self.assertEqual(parsed["title"], "Refactor the auth module")   # from the H1
        self.assertEqual(parsed["headings"], ["Context", "Approach"])   # H2 extraction
        self.assertEqual(parsed["n_steps"], 3)                          # numbered items
        self.assertGreater(parsed["words"], 0)

    def test_scan_plans_counts_and_norm(self):
        cfg = self._plans_dir()
        (cfg / "plans" / "a.md").write_text(self._PLAN, encoding="utf-8")
        (cfg / "plans" / "b.md").write_text(self._PLAN, encoding="utf-8")
        res = scan_plans(cfg, None)
        self.assertEqual(res["total"], 2)
        self.assertEqual(res["count"], 2)
        # two identical plan shapes share a normalized title (skill-chain signal).
        self.assertEqual(res["plans"][0]["norm"], res["plans"][1]["norm"])

    def test_since_filter_uses_mtime(self):
        cfg = self._plans_dir()
        p = cfg / "plans" / "plan.md"
        p.write_text(self._PLAN, encoding="utf-8")
        mtime_ms = int(p.stat().st_mtime * 1000)
        # cutoff in the future -> out of window (counted in total, not retained).
        future = scan_plans(cfg, since_ms=mtime_ms + 60_000)
        self.assertEqual(future["total"], 1)
        self.assertEqual(future["count"], 0)
        # cutoff in the past -> retained.
        past = scan_plans(cfg, since_ms=mtime_ms - 60_000)
        self.assertEqual(past["count"], 1)

    def test_heading_secret_scrubbed(self):
        cfg = self._plans_dir()
        p = cfg / "plans" / "leaky.md"
        p.write_text("# Deploy\n\n## Use token=%s here\n1. go\n" % TOKEN, encoding="utf-8")
        parsed = _parse_plan(p)
        self.assertNotIn(TOKEN, json.dumps(parsed), "token leaked from a plan heading")


class PreflightTests(unittest.TestCase):
    def _settings(self, cfg: Path, fname: str, obj: dict) -> None:
        (cfg / fname).write_text(json.dumps(obj), encoding="utf-8")

    def test_managed_policy_absent(self):
        self.assertEqual(_managed_hooks_policy(_tmp(self)),
                         {"active": False, "source": None})

    def test_managed_policy_only_base(self):
        cfg = _tmp(self)
        self._settings(cfg, "settings.json", {"allowManagedHooksOnly": True})
        pol = _managed_hooks_policy(cfg)
        self.assertTrue(pol["active"])
        self.assertEqual(pol["source"], "settings.json")

    def test_managed_policy_most_specific_wins(self):
        cfg = _tmp(self)
        # both a base and a remote settings file set the flag; the file read last
        # (remote-settings.json) must win as the reported source.
        self._settings(cfg, "settings.json", {"allowManagedHooksOnly": True})
        self._settings(cfg, "remote-settings.json", {"allowManagedHooksOnly": True})
        pol = _managed_hooks_policy(cfg)
        self.assertTrue(pol["active"])
        self.assertEqual(pol["source"], "remote-settings.json")

    def test_find_skill_present_and_absent(self):
        cfg = _tmp(self)
        sk = cfg / "skills" / "hookify-rules"
        sk.mkdir(parents=True)
        (sk / "SKILL.md").write_text("---\nname: hookify-rules\n---\n", encoding="utf-8")
        self.assertIsNotNone(_find_skill(cfg, "hookify-rules"))
        self.assertIsNone(_find_skill(cfg, "does-not-exist"))

    def test_preflight_detects_policy_and_optional_skill(self):
        cfg = _tmp(self)
        self._settings(cfg, "remote-settings.json", {"allowManagedHooksOnly": True})
        sk = cfg / "skills" / "continuous-learning-v2"
        sk.mkdir(parents=True)
        (sk / "SKILL.md").write_text("---\nname: continuous-learning-v2\n---\n", encoding="utf-8")
        env = preflight(cfg)
        self.assertTrue(env["config_dir_exists"])
        self.assertTrue(env["allow_managed_hooks_only"])
        self.assertEqual(env["managed_policy_source"], "remote-settings.json")
        self.assertIsNotNone(env["optional_skills"]["continuous-learning-v2"])
        self.assertTrue(any("allowManagedHooksOnly" in w for w in env["warnings"]),
                        "expected a managed-policy warning")


class TranscriptCapTests(unittest.TestCase):
    @staticmethod
    def _asst(tid: str) -> dict:
        return {"type": "assistant",
                "message": {"role": "assistant",
                            "content": [{"type": "tool_use", "id": tid,
                                         "name": "Read", "input": {"file_path": "x.py"}}]}}

    def _write_session(self, n: int) -> Path:
        path = _tmp(self) / "session.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            for i in range(n):
                fh.write(json.dumps(self._asst(f"t{i}")) + "\n")
        return path

    def test_max_lines_cap_stops_early(self):
        path = self._write_session(10)
        orig = scan_transcripts.MAX_LINES
        scan_transcripts.MAX_LINES = 3  # lines 1..3 processed; line 4 breaks (ln > cap)
        try:
            sess = scan_transcripts.extract_session(path)
        finally:
            scan_transcripts.MAX_LINES = orig
        self.assertEqual(sess["n_calls"], 3)
        self.assertEqual(sess["sequence"], ["Read", "Read", "Read"])

    def test_no_cap_reads_all(self):
        sess = scan_transcripts.extract_session(self._write_session(10))
        self.assertEqual(sess["n_calls"], 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
