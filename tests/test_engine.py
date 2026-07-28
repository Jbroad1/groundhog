#!/usr/bin/env python3
"""
Deterministic unit tests for the groundhog mining engine.

Pure stdlib `unittest` (no pytest dependency) so the suite runs on a vanilla
Python 3 install::

    python -m unittest discover -s tests -v
    # or
    python tests/test_engine.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import groundhog_lib as fl            # noqa: E402
from scrub import scrub_text       # noqa: E402
from mine_workflows import mine    # noqa: E402
from make_fixtures import build, PLANTED_SECRET  # noqa: E402


def _by_kind(scan, kind):
    return [c for c in scan["candidates"] if c["kind"] == kind]


def _first(cands, pred):
    for c in cands:
        if pred(c):
            return c
    return None


class ScrubTests(unittest.TestCase):
    def test_token_shapes_redacted(self):
        for raw in ["ghp_EXAMPLEONLYNOTAREALTOKEN00",
                    "sk-abcdefghijklmnopqrstuvwxyz012",
                    "AKIAABCDEFGHIJKLMNOP",
                    "token=supersecretvalue123"]:
            out, hits = scrub_text(raw)
            self.assertGreaterEqual(hits, 1, raw)
            self.assertNotIn(raw.split("=")[-1], out, f"secret survived: {raw}")

    def test_plain_text_untouched(self):
        out, hits = scrub_text("just refactor the auth module please")
        self.assertEqual(hits, 0)

    def test_punctuation_password_redacted(self):
        # Passwords with punctuation must be redacted whole, not left verbatim
        # because the value class stopped at the first special char.
        for raw, secret in [
            ("password: p@ss!w0rd#2024", "p@ss!w0rd#2024"),
            ('DB_PASSWORD="S3cr!t@#Pass"', "S3cr!t@#Pass"),
            ("passwd = $up3r#Secret!", "$up3r#Secret!"),
        ]:
            out, hits = scrub_text(raw)
            self.assertGreaterEqual(hits, 1, raw)
            self.assertNotIn(secret, out, f"secret survived: {raw!r} -> {out!r}")

    def test_url_credentials_redacted(self):
        # user:pass@host in a URL (git clone / DB DSN) is a very common leak.
        for raw, secret in [
            ("clone https://alice:supersecretpw@github.com/x/y.git", "supersecretpw"),
            ("postgresql://admin:Hunter2Hunter2@db.internal:5432/prod", "Hunter2Hunter2"),
        ]:
            out, hits = scrub_text(raw)
            self.assertGreaterEqual(hits, 1, raw)
            self.assertNotIn(secret, out, f"credential survived: {raw!r} -> {out!r}")

    def test_url_password_with_at_redacted(self):
        # P1c: a password containing '@' must be redacted in full. The old
        # password class [^\s:/@]+ stopped at the FIRST '@' and leaked the tail
        # (postgres://user:p@ssw0rd@host -> ...:[REDACTED]@ssw0rd@host). The
        # greedy [^\s/]+ now backtracks to the real @host and redacts the whole
        # password. `leaked_tail` is the fragment that survived under the old bug.
        for raw, leaked_tail, host in [
            ("postgres://user:p@ssw0rd@host", "ssw0rd", "@host"),
            ("mysql://u:p@ss@w0rd@db:3306/x", "w0rd", "@db:3306"),
        ]:
            out, hits = scrub_text(raw)
            self.assertGreaterEqual(hits, 1, raw)
            self.assertNotIn(leaked_tail, out, f"password tail survived: {raw!r} -> {out!r}")
            self.assertIn(host, out, f"real host token not preserved: {out!r}")

    def test_url_without_creds_untouched(self):
        # A URL with a port and an '@' only in the query string (an email) has no
        # userinfo credentials; the scrubber must not touch it -- the greedy
        # password class cannot reach the query '@' across the path '/'.
        raw = "https://host:443/api?email=a@b.com"
        out, hits = scrub_text(raw)
        self.assertEqual(hits, 0, f"false-positive redaction: {raw!r} -> {out!r}")
        self.assertEqual(out, raw)

    def test_url_password_host_omitted_redacted(self):
        # A URL whose authority omits the host -- the '@' is followed by '/',
        # whitespace, or end-of-string -- must still have its password redacted.
        # A trailing group that requires a host character leaks these; the empty-
        # host-tolerant (@[^\s@/]*) redacts them. The postgresql case is a real
        # Unix-domain-socket DSN (host omitted, socket dir passed as a parameter).
        for raw, secret in [
            ("postgresql://user:secret@/dbname?host=/var/run/postgresql", "secret"),
            ("redis://user:pass@", "pass"),
        ]:
            out, hits = scrub_text(raw)
            self.assertGreaterEqual(hits, 1, raw)
            self.assertNotIn(secret, out, f"host-omitted DSN leaked password: {raw!r} -> {out!r}")

    def test_long_scheme_like_input_scrubs_without_redos(self):
        # ReDoS guard: the url-credentials scheme repeat is length-bounded, so a
        # long run of scheme-like characters containing no '://' scrubs in linear
        # time. The old unbounded prefix was O(n^2) (~minutes on a 200k-char blob);
        # the generous 5s ceiling flags a regression without flaking on the ~ms
        # the bounded pattern actually takes.
        import time
        blob = "a1b2c3d4+." * 20_000  # 200k chars of [A-Za-z0-9+.-], no '://'
        start = time.perf_counter()
        out, hits = scrub_text(blob)
        elapsed = time.perf_counter() - start
        self.assertEqual(hits, 0, "no secret shape present; expected no redactions")
        self.assertEqual(out, blob)
        self.assertLess(elapsed, 5.0, f"scrub_text O(n^2) ReDoS regression? took {elapsed:.2f}s")

    def test_long_private_key_redacted(self):
        # A key body larger than the old 4000-char cap must still be redacted
        # (the cap used to fail open and pass the whole key through).
        body = "MIIB" + "A" * 6000
        key = "-----BEGIN RSA PRIVATE KEY-----\n" + body + "\n-----END RSA PRIVATE KEY-----"
        out, hits = scrub_text(key)
        self.assertGreaterEqual(hits, 1)
        self.assertNotIn("AAAA", out, "long private key survived the cap")

    def test_json_quoted_key_redacted(self):
        # JSON-shaped secrets (quoted key) are common in mined tool inputs/results.
        for raw, secret in [
            ('{"password": "p@ss!val"}', "p@ss!val"),
            ('"api_key":"secretValue123!"', "secretValue123!"),
        ]:
            out, hits = scrub_text(raw)
            self.assertGreaterEqual(hits, 1, raw)
            self.assertNotIn(secret, out, f"secret survived: {raw!r} -> {out!r}")

    def test_keyless_token_is_known_limitation(self):
        # DOCUMENTED LIMITATION: a bare high-entropy token with no keyword and no
        # known prefix is NOT redacted by shape rules. Pinned so any future change
        # is deliberate, and so the README's "best-effort, review before sharing"
        # wording stays honest.
        out, hits = scrub_text("d41d8cd98f00b204e9800998ecf8427e1a2b3c4d")
        self.assertEqual(hits, 0)


class LibTests(unittest.TestCase):
    def test_template_bash(self):
        self.assertEqual(fl.template_bash('git commit -m "fix: x"'), "git commit -m <STR>")
        self.assertEqual(fl.template_bash("pytest tests/ -q"), "pytest tests/ -q")

    def test_repo_relative(self):
        self.assertEqual(fl.repo_relative("C:/work/proj-a/src/app.py", "C:/work/proj-a"),
                         "src/app.py")
        self.assertEqual(fl.repo_relative("/other/x.py", "C:/work/proj-a"), "x.py")

    def test_parse_since(self):
        self.assertIsNone(fl.parse_since("all"))
        self.assertIsNotNone(fl.parse_since("30d"))
        self.assertIsNotNone(fl.parse_since("2026-01-01"))

    def test_frontmatter(self):
        meta, body = fl.parse_frontmatter("---\nname: x\ndescription: Use when y\n---\nbody\n")
        self.assertEqual(meta["name"], "x")
        self.assertEqual(meta["description"], "Use when y")
        self.assertIn("body", body)

    def test_frontmatter_with_bom(self):
        meta, _ = fl.parse_frontmatter("﻿---\nname: x\ndescription: Use when y\n---\nbody\n")
        self.assertEqual(meta.get("name"), "x")

    def test_iso_odd_fractional_seconds(self):
        # fromisoformat pre-3.11 accepts only 3/6-digit fractions; short (.12) and
        # long (>6) fractional seconds must both normalize and parse (not None).
        base = fl.to_epoch_ms("2026-07-01T00:00:00Z")
        self.assertEqual(fl.to_epoch_ms("2026-07-01T00:00:00.000000Z"), base)
        self.assertIsNotNone(fl.to_epoch_ms("2026-07-01T00:00:00.12Z"))
        self.assertIsNotNone(fl.to_epoch_ms("2026-07-01T00:00:00.123456789Z"))
        self.assertIsNotNone(fl.parse_since("2026-07-01T00:00:00.12Z"))


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.cfg = build(Path(cls._tmp.name) / "cfg")
        cls.scan = mine(cls.cfg, since_ms=None, cache={})

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_sources_counted(self):
        self.assertEqual(self.scan["sources"]["transcripts"]["sessions"], 9)
        self.assertTrue(self.scan["sources"]["history"]["count"] >= 6)
        self.assertEqual(self.scan["sources"]["plans"]["count"], 2)

    def test_tool_sequence_detected(self):
        seq = _first(_by_kind(self.scan, "tool-sequence"),
                     lambda c: c["signature"] == ["Grep", "Read", "Edit"])
        self.assertIsNotNone(seq, "Grep->Read->Edit not detected")
        self.assertGreaterEqual(seq["frequency"], 4)
        self.assertGreater(seq["score"], 0)
        self.assertIn(seq["recommended_primitive"], ("skill", "skill-chain"))

    def test_edit_hotspot_detected(self):
        hot = _first(_by_kind(self.scan, "edit-hotspot"),
                     lambda c: c["signature"] == ["edit", "src/app.py"])
        self.assertIsNotNone(hot, "src/app.py hotspot not detected")
        self.assertEqual(hot["evidence"]["edit_count"], 4)

    def test_bash_guardrail_and_managed_policy(self):
        # default: a guardrail Bash template recommends a hook
        bash = _first(_by_kind(self.scan, "bash-template"), lambda c: "pytest" in c["title"])
        self.assertIsNotNone(bash, "pytest template not detected")
        self.assertTrue(bash["evidence"]["guardish"])
        self.assertEqual(bash["recommended_primitive"], "hook")
        self.assertFalse(bash["managed_hook_warning"])

        # with allowManagedHooksOnly, it must fall back to a hookify-rule + warning
        managed = mine(self.cfg, since_ms=None, cache={},
                       env={"allow_managed_hooks_only": True})
        b2 = _first(_by_kind(managed, "bash-template"), lambda c: "pytest" in c["title"])
        self.assertEqual(b2["recommended_primitive"], "hookify-rule")
        self.assertTrue(b2["managed_hook_warning"])

    def test_error_fix_paired(self):
        ef = _first(_by_kind(self.scan, "error-fix"),
                    lambda c: c["signature"] == ["errfix", "Bash", "Edit"])
        self.assertIsNotNone(ef, "Bash-error -> Edit not paired")
        self.assertGreaterEqual(ef["evidence"]["pair_count"], 3)

    def test_repeated_call_loop(self):
        loop = _first(_by_kind(self.scan, "repeated-call-loop"),
                      lambda c: c["signature"][0] == "Read")
        self.assertIsNotNone(loop, "Read loop not detected")
        self.assertGreaterEqual(loop["evidence"]["max_run"], 4)

    def test_secret_scrubbed_everywhere(self):
        dumped = json.dumps(self.scan)
        self.assertNotIn(PLANTED_SECRET, dumped, "PLANTED SECRET leaked into scan.json")
        self.assertIn("REDACTED", dumped, "expected a redaction marker somewhere")

    def test_env_metadata_scrubbed(self):
        # B2: scrub_obj now walks the whole scan dict, so a token planted in the
        # env/config_dir metadata (not just candidate payloads) is redacted.
        # Before the fix only scan["candidates"] was scrubbed and this leaked.
        planted = mine(self.cfg, since_ms=None, cache={},
                       env={"leaky_metadata": "deploy token " + PLANTED_SECRET})
        dumped = json.dumps(planted)
        self.assertNotIn(PLANTED_SECRET, dumped,
                         "token in env metadata leaked into scan.json (B2)")
        val = planted["env"]["leaky_metadata"]
        self.assertNotIn(PLANTED_SECRET, val)
        self.assertIn("REDACTED", val, f"env value not scrubbed: {val!r}")

    def test_slash_cluster_flagged_already_automated(self):
        slash = _first(_by_kind(self.scan, "prompt-cluster"),
                       lambda c: c["evidence"].get("is_slash"))
        self.assertIsNotNone(slash, "slash cluster not found")
        self.assertEqual(slash["already_automated"], 1.0)
        self.assertEqual(slash["score"], 0.0)

    def test_dedup_raises_already_automated(self):
        bash = _first(_by_kind(self.scan, "bash-template"), lambda c: "pytest" in c["title"])
        self.assertGreater(bash["already_automated"], 0.0,
                           "existing pytest-runner skill should raise already_automated")

    def test_plan_type_detected(self):
        plan = _first(_by_kind(self.scan, "plan-type"), lambda c: "refactor" in c["title"].lower())
        self.assertIsNotNone(plan, "recurring plan shape not detected")
        self.assertEqual(plan["recommended_primitive"], "skill-chain")

    def test_proof_paths_point_to_real_files(self):
        seq = _first(_by_kind(self.scan, "tool-sequence"),
                     lambda c: c["signature"] == ["Grep", "Read", "Edit"])
        self.assertTrue(seq["proof_paths"], "no proof paths")
        for pp in seq["proof_paths"]:
            self.assertTrue(Path(pp["file"]).exists(), f"proof file missing: {pp['file']}")
            self.assertIsInstance(pp["line"], int)

    def test_deterministic(self):
        a = mine(self.cfg, since_ms=None, cache={})
        b = mine(self.cfg, since_ms=None, cache={})
        self.assertEqual(json.dumps(a["candidates"]), json.dumps(b["candidates"]),
                         "candidate ordering / content is not deterministic")

    def test_incremental_cache(self):
        cache = {}
        first = mine(self.cfg, since_ms=None, cache=cache)
        self.assertEqual(first["sources"]["transcripts"]["files_parsed"], 9)
        self.assertEqual(first["sources"]["transcripts"]["cache_hits"], 0)
        second = mine(self.cfg, since_ms=None, cache=cache)
        self.assertEqual(second["sources"]["transcripts"]["files_parsed"], 0)
        self.assertEqual(second["sources"]["transcripts"]["cache_hits"], 9)


class CacheTests(unittest.TestCase):
    def _cfg(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        return build(Path(self._tmp.name) / "cfg")

    def test_cache_schema_version_invalidates(self):
        from scan_transcripts import scan_transcripts
        cfg = self._cfg()
        cache = {}
        first = scan_transcripts(cfg, None, cache)
        self.assertEqual(first["files_parsed"], 9)
        self.assertEqual(first["cache_hits"], 0)
        second = scan_transcripts(cfg, None, cache)
        self.assertEqual(second["cache_hits"], 9)
        self.assertEqual(second["files_parsed"], 0)
        # simulate a parser upgrade: stored version no longer matches
        for v in cache.values():
            v["v"] = "OLD"
        third = scan_transcripts(cfg, None, cache)
        self.assertEqual(third["files_parsed"], 9, "stale-version entries not re-parsed")

    def test_cache_prunes_deleted_transcripts(self):
        from scan_transcripts import scan_transcripts
        cfg = self._cfg()
        cache = {"C:/gone/old-session.jsonl":
                 {"v": fl.SCHEMA_VERSION, "mtime": 1, "size": 1, "result": {}}}
        scan_transcripts(cfg, None, cache)
        self.assertNotIn("C:/gone/old-session.jsonl", cache,
                         "cache entry for a deleted transcript was not pruned")


if __name__ == "__main__":
    unittest.main(verbosity=2)
