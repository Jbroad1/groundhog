#!/usr/bin/env python3
"""
WS4 tests: the durable, crash-safe, remembering scan.

Covers the IndexStore (change detection, mtime-drift immunity, schema migration,
corrupt-db fallback, verdict ledger), the index-backed incremental scan (unchanged
skipped, appends folded via byte-offset resume, crash-safe per-session commit),
and the end-to-end verdict memory (remember -> re-scan surfaces the prior verdict).

Pure stdlib `unittest`, matching the other suites.
"""
from __future__ import annotations

import gc
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import index_store                                  # noqa: E402
from index_store import IndexStore                  # noqa: E402
from scan_transcripts import scan_transcripts       # noqa: E402
import groundhog                                     # noqa: E402
from make_fixtures import build                      # noqa: E402


def _box(case) -> Path:
    d = tempfile.mkdtemp()
    case.addCleanup(shutil.rmtree, d, ignore_errors=True)
    return Path(d)


class IndexStoreTests(unittest.TestCase):
    def _store(self):
        box = _box(self)
        store = IndexStore.open(box)
        self.addCleanup(store.close)
        return store, box

    def test_classify_states(self):
        st, _ = self._store()
        self.assertEqual(st.classify("a.jsonl", 100, 5)[0], "new")
        st.upsert_session("a.jsonl", 100, 5, 100, "h", {"file": "a.jsonl"})
        self.assertEqual(st.classify("a.jsonl", 100, 5), ("unchanged", 100))
        self.assertEqual(st.classify("a.jsonl", 150, 6)[0], "appended")
        self.assertEqual(st.classify("a.jsonl", 50, 6)[0], "modified")

    def test_mtime_drift_immunity(self):
        # OneDrive rewrites mtime without touching content: same size + same
        # prefix-hash must read as unchanged; a same-size *rewrite* as modified.
        st, box = self._store()
        f = box / "s.jsonl"
        f.write_bytes(b'{"x":1}\n')
        size = f.stat().st_size
        ph = index_store.head_hash(str(f), min(4096, size))
        st.upsert_session(str(f), size, 1000, size, ph, {"file": str(f)})
        self.assertEqual(st.classify(str(f), size, 9999)[0], "unchanged",
                         "mtime drift with identical content should not re-parse")
        f.write_bytes(b'{"y":9}\n')  # same length, different bytes
        self.assertEqual(st.classify(str(f), size, 9999)[0], "modified",
                         "a same-size rewrite must be caught by the prefix hash")

    def test_verdict_ledger_roundtrip(self):
        st, _ = self._store()
        st.upsert_verdict(["Grep", "Read", "Edit"], "skill", "keep", "repeated flow", 0.8, "fp1")
        row = st.get_verdict(["Grep", "Read", "Edit"])
        self.assertEqual(row["primitive"], "skill")
        self.assertEqual(row["evidence_fingerprint"], "fp1")
        # a list and its joined form address the same row (merge, not duplicate)
        st.upsert_verdict("Grep|Read|Edit", "hook", "keep", "changed my mind", 0.9, "fp2")
        self.assertEqual(len(st.all_verdicts()), 1)
        self.assertEqual(st.get_verdict(["Grep", "Read", "Edit"])["primitive"], "hook")

    def test_meta_and_fingerprint(self):
        st, _ = self._store()
        st.set_meta("last_run_epoch", 123)
        self.assertEqual(st.get_meta("last_run_epoch"), "123")
        st.upsert_session("a.jsonl", 10, 1, 10, "h", {"file": "a.jsonl"})
        fp1 = st.corpus_fingerprint()
        st.upsert_session("b.jsonl", 20, 2, 20, "h", {"file": "b.jsonl"})
        self.assertNotEqual(fp1, st.corpus_fingerprint(), "fingerprint ignored a new session")


class IndexResilienceTests(unittest.TestCase):
    def _db(self, box):
        return box / "groundhog" / "index.db"

    def test_schema_bump_discards_and_rescans(self):
        box = _box(self)
        st = IndexStore.open(box)
        st.upsert_session("a.jsonl", 1, 1, 1, "h", {"file": "a.jsonl"})
        st.close()
        con = sqlite3.connect(str(self._db(box)))
        con.execute("PRAGMA user_version=999")  # incompatible schema
        con.commit(); con.close()
        st2 = IndexStore.open(box)
        self.addCleanup(st2.close)
        self.assertEqual(st2.count_sessions(), 0, "schema bump did not discard the old index")

    def test_corrupt_db_quarantined_and_rebuilt(self):
        box = _box(self)
        st = IndexStore.open(box)
        st.upsert_session("a.jsonl", 1, 1, 1, "h", {"file": "a.jsonl"})
        st.close()
        self._db(box).write_bytes(b"definitely not a sqlite database \x00\x01\x02")
        st2 = IndexStore.open(box)
        self.addCleanup(st2.close)
        self.assertTrue(self._db(box).with_name("index.db.bak").exists(),
                        "corrupt db was not preserved as .db.bak")
        self.assertEqual(st2.count_sessions(), 0, "corrupt db was not rebuilt fresh")


class IncrementalScanTests(unittest.TestCase):
    def _cfg(self):
        return build(_box(self) / "cfg")

    def test_unchanged_files_are_skipped(self):
        cfg = self._cfg()
        st = IndexStore.open(cfg)
        self.addCleanup(st.close)
        first = scan_transcripts(cfg, None, store=st)
        self.assertEqual(first["files_parsed"], 9)
        self.assertEqual(first["cache_hits"], 0)
        self.assertEqual(len(first["sessions"]), 9)
        second = scan_transcripts(cfg, None, store=st)
        self.assertEqual(second["files_parsed"], 0, "unchanged files were re-parsed")
        self.assertEqual(second["cache_hits"], 9)
        self.assertEqual(len(second["sessions"]), 9)

    def test_mtime_drift_does_not_reparse(self):
        cfg = self._cfg()
        st = IndexStore.open(cfg)
        self.addCleanup(st.close)
        scan_transcripts(cfg, None, store=st)
        target = cfg / "projects" / "C--work-proj-a" / "seqA1.jsonl"
        stt = target.stat()
        os.utime(target, (stt.st_atime, stt.st_mtime + 500))  # move mtime, same content
        res = scan_transcripts(cfg, None, store=st)
        self.assertEqual(res["files_parsed"], 0, "OneDrive mtime drift forced a needless re-parse")
        self.assertEqual(res["cache_hits"], 9)

    def test_append_is_folded_incrementally(self):
        cfg = self._cfg()
        st = IndexStore.open(cfg)
        self.addCleanup(st.close)
        scan_transcripts(cfg, None, store=st)
        target = cfg / "projects" / "C--work-proj-a" / "seqA1.jsonl"
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "type": "assistant", "sessionId": "seqA1",
                "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "z", "name": "Bash",
                     "input": {"command": "echo appended"}}]}}) + "\n")
        res = scan_transcripts(cfg, None, store=st)
        self.assertEqual(res["files_parsed"], 1, "only the appended file should re-parse")
        self.assertEqual(res["cache_hits"], 8)
        summ = json.loads(st.get_session(str(target))["summary_json"])
        self.assertEqual(summ["sequence"], ["Grep", "Read", "Edit", "Bash"],
                         "the appended call was not folded onto the stored summary")

    def test_append_guard_rejects_net_larger_rewrite(self):
        # A larger file is trusted as an append ONLY if its start is intact. A
        # net-larger REWRITE (different early bytes) must be "modified", or the
        # delta parse would resume at a stale offset and corrupt the summary.
        cfg = self._cfg()
        st = IndexStore.open(cfg)
        self.addCleanup(st.close)
        target = cfg / "projects" / "C--work-proj-a" / "seqA1.jsonl"
        scan_transcripts(cfg, None, store=st)             # index it
        orig = target.read_bytes()
        # true append: same start, larger -> "appended"
        target.write_bytes(orig + b'{"type":"user","message":{"content":[]}}\n')
        s = target.stat()
        self.assertEqual(st.classify(str(target), s.st_size, int(s.st_mtime * 1000) + 1)[0],
                         "appended", "a true append with an intact start was not detected")
        # net-larger rewrite with different early bytes -> "modified"
        target.write_bytes(b'{"REWRITTEN":true}\n' + orig + orig)
        s = target.stat()
        self.assertEqual(st.classify(str(target), s.st_size, int(s.st_mtime * 1000) + 2)[0],
                         "modified", "a net-larger rewrite was mistaken for an append")

    def test_deleted_file_is_pruned(self):
        cfg = self._cfg()
        st = IndexStore.open(cfg)
        self.addCleanup(st.close)
        scan_transcripts(cfg, None, store=st)
        self.assertEqual(st.count_sessions(), 9)
        (cfg / "projects" / "C--work-proj-a" / "loop1.jsonl").unlink()
        scan_transcripts(cfg, None, store=st)
        self.assertEqual(st.count_sessions(), 8, "a deleted transcript was not pruned")

    def test_crash_safe_partial_progress(self):
        # Per-session commits mean a crash (no final close) still leaves a valid,
        # partial index that the next run resumes from.
        cfg = self._cfg()
        st = IndexStore.open(cfg)
        st.upsert_session("p1.jsonl", 10, 1, 10, "h", {"file": "p1.jsonl", "sequence": []})
        st.upsert_session("p2.jsonl", 20, 2, 20, "h", {"file": "p2.jsonl", "sequence": []})
        del st            # simulate a crash: no orderly close
        gc.collect()      # force the connection's release so the WAL lock frees on Windows
        st2 = IndexStore.open(cfg)
        self.addCleanup(st2.close)
        self.assertIsNotNone(st2.get_session("p1.jsonl"))
        self.assertIsNotNone(st2.get_session("p2.jsonl"))


class VerdictMemoryTests(unittest.TestCase):
    def _run(self, argv):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = groundhog.main(argv)
        return rc, buf.getvalue()

    def test_prior_verdict_surfaced_on_rescan(self):
        box = _box(self)
        cfg = build(box / "cfg")
        scan_path = box / "scan.json"
        self._run(["scan", "--config-dir", str(cfg), "--since", "all", "--out", str(scan_path)])
        scan = json.loads(scan_path.read_text(encoding="utf-8"))
        cand = next(c for c in scan["candidates"] if c["signature"] == ["Grep", "Read", "Edit"])
        self.assertIn("evidence_fingerprint", cand, "index scan did not fingerprint candidates")

        verdict = {"proposals": [{"candidate_id": cand["id"], "primitive": "skill",
                                  "confidence": 0.8, "rationale": "a repeated flow"}],
                   "dropped": []}
        vpath = box / "verdict.json"
        vpath.write_text(json.dumps(verdict), encoding="utf-8")
        rc, _ = self._run(["remember", "--config-dir", str(cfg),
                           "--verdict", str(vpath), "--scan", str(scan_path)])
        self.assertEqual(rc, 0)

        scan2_path = box / "scan2.json"
        self._run(["scan", "--config-dir", str(cfg), "--since", "all", "--out", str(scan2_path)])
        scan2 = json.loads(scan2_path.read_text(encoding="utf-8"))
        cand2 = next(c for c in scan2["candidates"] if c["signature"] == ["Grep", "Read", "Edit"])
        self.assertIn("prior_verdict", cand2, "the prior verdict was not remembered on re-scan")
        self.assertEqual(cand2["prior_verdict"]["primitive"], "skill")
        self.assertTrue(cand2["prior_verdict"]["evidence_unchanged"],
                        "unchanged evidence should be recognised as such")

    def test_remember_without_scan_via_signature(self):
        # Robust path (C3): a verdict whose proposals carry their OWN signature +
        # evidence_fingerprint is remembered WITHOUT --scan, even with a stale
        # candidate_id, and a re-scan surfaces prior_verdict.
        box = _box(self)
        cfg = build(box / "cfg")
        scan_path = box / "scan.json"
        self._run(["scan", "--config-dir", str(cfg), "--since", "all", "--out", str(scan_path)])
        scan = json.loads(scan_path.read_text(encoding="utf-8"))
        cand = next(c for c in scan["candidates"] if c["signature"] == ["Grep", "Read", "Edit"])
        verdict = {"proposals": [{"candidate_id": "cand-9999",   # deliberately stale/wrong id
                                  "signature": cand["signature"],
                                  "evidence_fingerprint": cand["evidence_fingerprint"],
                                  "primitive": "hook", "confidence": 0.9,
                                  "rationale": "recurring guardrail"}],
                   "dropped": []}
        vpath = box / "verdict.json"
        vpath.write_text(json.dumps(verdict), encoding="utf-8")
        rc, _ = self._run(["remember", "--config-dir", str(cfg), "--verdict", str(vpath)])  # NO --scan
        self.assertEqual(rc, 0)
        scan2_path = box / "scan2.json"
        self._run(["scan", "--config-dir", str(cfg), "--since", "all", "--out", str(scan2_path)])
        scan2 = json.loads(scan2_path.read_text(encoding="utf-8"))
        cand2 = next(c for c in scan2["candidates"] if c["signature"] == ["Grep", "Read", "Edit"])
        self.assertIn("prior_verdict", cand2, "signature-keyed verdict not remembered without --scan")
        self.assertEqual(cand2["prior_verdict"]["primitive"], "hook")
        self.assertTrue(cand2["prior_verdict"]["evidence_unchanged"])

    def test_remember_tolerates_malformed_verdict(self):
        # Untrusted verdict.json (C1/C2): non-dict entries and a non-numeric
        # confidence must not crash the ledger write.
        box = _box(self)
        cfg = build(box / "cfg")
        verdict = {"proposals": ["not-a-dict",
                                 {"signature": ["A", "B"], "primitive": "skill",
                                  "confidence": "high", "rationale": "x"}],
                   "dropped": [42, {"signature": ["C"], "reason": "dupe"}]}
        vpath = box / "verdict.json"
        vpath.write_text(json.dumps(verdict), encoding="utf-8")
        rc, _ = self._run(["remember", "--config-dir", str(cfg), "--verdict", str(vpath)])
        self.assertEqual(rc, 0, "remember crashed on a malformed verdict.json")


if __name__ == "__main__":
    unittest.main(verbosity=2)
