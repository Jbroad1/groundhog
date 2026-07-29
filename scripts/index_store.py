#!/usr/bin/env python3
"""
index_store.py -- groundhog's durable, crash-safe SQLite index.

Supersedes the single giant ``cache.json``. One database at
``<config>/groundhog/index.db`` holds three layers of memory so re-runs stay fast
and bounded on a tree that can reach ~1M transcripts:

  * ``sessions``   -- scan-layer memory: one row per transcript with the parse
                      summary, so an unchanged file is never re-read.
  * ``aggregates`` -- mine-layer rollup (kind/signature -> counts + proofs),
                      materialised each run from the in-window session summaries.
  * ``verdicts``   -- analyze-layer ledger: what we decided about a signature last
                      time, so a re-run remembers instead of re-asking.
  * ``meta``       -- small key/value store (e.g. ``last_run_epoch``).

Durability model (git stat-cache / Watchman / Spark-checkpoint lineage):
  * WAL journalling + ``synchronous=NORMAL`` -- cheap per-session commits, no
    torn writes.
  * ``user_version`` gates migrations: a schema bump discards the index and forces
    one slow rescan (never silently reuses an incompatible shape).
  * A corrupt database is renamed ``.db.bak`` and rebuilt from scratch, never
    read through.

Change detection is **size-primary** so it is immune to OneDrive rewriting mtime
without touching content (abraunegg/onedrive#3146): transcripts only ever grow, so
a larger size is an append and an equal size with a different mtime is settled by a
cheap prefix hash.

Pure stdlib (``sqlite3`` ships with CPython). Importable and self-checking.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import groundhog_lib as fl

SCHEMA_VERSION = 1  # bump -> discard the index + one slow full rescan

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    path            TEXT PRIMARY KEY,
    size            INTEGER NOT NULL,
    mtime           INTEGER NOT NULL,
    last_offset     INTEGER NOT NULL DEFAULT 0,
    prefix_hash     TEXT,
    summary_json    TEXT NOT NULL,
    last_scanned_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_mtime ON sessions(mtime);
CREATE TABLE IF NOT EXISTS aggregates (
    kind        TEXT NOT NULL,
    signature   TEXT NOT NULL,
    sessions    INTEGER NOT NULL,
    occurrences INTEGER NOT NULL,
    steps_saved INTEGER NOT NULL,
    proof_json  TEXT,
    PRIMARY KEY (kind, signature)
);
CREATE TABLE IF NOT EXISTS verdicts (
    signature            TEXT PRIMARY KEY,
    primitive            TEXT,
    decision             TEXT,
    reason               TEXT,
    confidence           REAL,
    evidence_fingerprint TEXT,
    decided_at           TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

_TABLES = ("sessions", "aggregates", "verdicts", "meta")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sig_key(signature) -> str:
    """Canonical ledger key for a candidate signature. Lists join on '|' (the same
    idiom the miner uses for its stable tie-break), so a list and its joined form
    address the same verdict row."""
    if isinstance(signature, (list, tuple)):
        return "|".join(map(str, signature))
    return str(signature)


def head_hash(path, nbytes: int = 4096) -> str:
    """sha1 of the first ``nbytes`` bytes of a file. Size-INDEPENDENT on purpose,
    so the same value verifies two things: (a) a same-size file's content is
    unchanged (mtime-drift tiebreak), and (b) a larger file's *start* is intact, so
    a size increase is a true append rather than a rewrite. Empty on read error.
    Never on the hot path for the common unchanged file."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(max(0, nbytes))
    except OSError:
        head = b""
    return hashlib.sha1(head).hexdigest()


class IndexStore:
    """Thin, well-scoped wrapper around the SQLite index. One instance per run."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.con = sqlite3.connect(str(self.db_path))
        self.con.row_factory = sqlite3.Row

    # -- lifecycle ---------------------------------------------------------- #
    @classmethod
    def open(cls, config_dir: Path) -> "IndexStore":
        """Open (or create) the index for a config dir. A corrupt or
        schema-incompatible database is preserved as ``.db.bak`` and rebuilt."""
        state = fl.ensure_dir(fl.state_dir(config_dir))
        db_path = state / "index.db"
        store = None
        try:
            store = cls(db_path)
            store._init()
            return store
        except sqlite3.DatabaseError as e:
            # A locked/busy database is transient (a concurrent scan, a stale
            # lock) -- NOT corruption. Never quarantine a live DB; surface it.
            # Only genuine corruption ("file is not a database" / "malformed")
            # gets moved aside and rebuilt.
            if any(w in str(e).lower() for w in ("lock", "busy")):
                if store is not None:
                    try: store.con.close()
                    except Exception: pass
                raise
            if store is not None:
                try: store.con.close()  # release the handle or the rename locks on Windows
                except Exception: pass
            cls._quarantine(db_path)
            store = cls(db_path)
            try:
                store._init(force_fresh=True)
            except Exception:
                try: store.con.close()
                except Exception: pass
                raise
            return store

    @staticmethod
    def _quarantine(db_path: Path) -> None:
        """Move a corrupt database (and its WAL/SHM siblings) aside as .db.bak."""
        try:
            if db_path.exists():
                bak = db_path.with_name(db_path.name + ".bak")
                if bak.exists():
                    bak.unlink()
                db_path.replace(bak)
            for ext in ("-wal", "-shm"):
                sib = Path(str(db_path) + ext)
                if sib.exists():
                    sib.unlink()
        except OSError:
            pass

    def _init(self, force_fresh: bool = False) -> None:
        con = self.con
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        ver = con.execute("PRAGMA user_version").fetchone()[0]
        if force_fresh or (ver not in (0, SCHEMA_VERSION)):
            # Schema bump or forced rebuild: discard everything. One slow rescan
            # follows -- correct beats silently reusing an incompatible shape.
            for t in _TABLES:
                con.execute(f"DROP TABLE IF EXISTS {t}")
            ver = 0
        if ver == 0:
            con.executescript(_SCHEMA)
            con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        con.commit()

    def close(self) -> None:
        try:
            self.con.commit()
        finally:
            self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- sessions (scan-layer memory) --------------------------------------- #
    def get_session(self, path: str):
        return self.con.execute("SELECT * FROM sessions WHERE path=?", (str(path),)).fetchone()

    def classify(self, path: str, size: int, mtime: int):
        """Return (state, resume_offset) for a file given its current size/mtime.

        states: 'new' | 'unchanged' | 'appended' | 'modified'. Size-primary so
        OneDrive mtime drift never forces a needless re-parse."""
        row = self.get_session(path)
        if row is None:
            return "new", 0
        stored_ph = row["prefix_hash"] or ""
        if size == row["size"]:
            if mtime == row["mtime"]:
                return "unchanged", row["last_offset"]
            # same size, moved mtime: OneDrive drift vs a same-size rewrite.
            if head_hash(path, min(4096, size)) == stored_ph:
                return "unchanged", row["last_offset"]
            return "modified", 0
        if size > row["size"]:
            # A larger file is an append ONLY if the stored prefix is still intact.
            # A net-larger REWRITE (different early bytes) must full re-parse, or
            # extract_or_extend would resume at a stale offset and splice garbage
            # onto the old summary. If the head is unreadable we fall back to the
            # append-only assumption (the transcript norm). Hashing min(4096,
            # stored_size) bytes keeps this correct for small files too.
            if stored_ph and Path(path).exists():
                if head_hash(path, min(4096, row["size"])) == stored_ph:
                    return "appended", row["last_offset"]
                return "modified", 0
            return "appended", row["last_offset"]
        return "modified", 0  # shrank -> truncated / rewritten

    def upsert_session(self, path, size, mtime, last_offset, phash, summary) -> None:
        """Write one session row and commit (per-session commit = crash-safe
        partial progress). WAL + synchronous=NORMAL keeps this cheap."""
        self.con.execute(
            "INSERT OR REPLACE INTO sessions"
            "(path,size,mtime,last_offset,prefix_hash,summary_json,last_scanned_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (str(path), int(size), int(mtime), int(last_offset), phash,
             json.dumps(summary, ensure_ascii=False), _now_iso()))
        self.con.commit()

    def iter_summaries(self, since_ms=None):
        """Yield session summaries in the window, ordered by path (deterministic).
        Streams from the DB -- never holds every raw transcript in memory."""
        if since_ms is None:
            cur = self.con.execute("SELECT summary_json FROM sessions ORDER BY path")
        else:
            cur = self.con.execute(
                "SELECT summary_json FROM sessions WHERE mtime >= ? ORDER BY path",
                (int(since_ms),))
        for (sj,) in cur:
            try:
                yield json.loads(sj)
            except (ValueError, TypeError):
                continue

    def count_sessions(self, since_ms=None) -> int:
        if since_ms is None:
            return self.con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        return self.con.execute("SELECT COUNT(*) FROM sessions WHERE mtime >= ?",
                                (int(since_ms),)).fetchone()[0]

    def prune_missing(self, seen_paths) -> int:
        """Delete rows for transcripts no longer on disk. ``seen_paths`` is every
        .jsonl path found this walk (in and out of window), so out-of-window
        sessions are kept, only truly-deleted files are dropped."""
        seen = set(map(str, seen_paths))
        gone = [r[0] for r in self.con.execute("SELECT path FROM sessions")
                if r[0] not in seen]
        if gone:
            self.con.executemany("DELETE FROM sessions WHERE path=?", ((p,) for p in gone))
            self.con.commit()
        return len(gone)

    # -- aggregates (mine-layer rollup) ------------------------------------- #
    def replace_aggregates(self, rows) -> None:
        """Materialise the current mine-layer rollup. ``rows`` is an iterable of
        (kind, signature, sessions, occurrences, steps_saved, proof_obj)."""
        self.con.execute("DELETE FROM aggregates")
        self.con.executemany(
            "INSERT OR REPLACE INTO aggregates"
            "(kind,signature,sessions,occurrences,steps_saved,proof_json) VALUES(?,?,?,?,?,?)",
            [(k, s, int(se), int(o), int(st), json.dumps(p, ensure_ascii=False))
             for (k, s, se, o, st, p) in rows])
        self.con.commit()

    # -- verdict ledger (memory of prior decisions) ------------------------- #
    def get_verdict(self, signature):
        return self.con.execute("SELECT * FROM verdicts WHERE signature=?",
                                (sig_key(signature),)).fetchone()

    def upsert_verdict(self, signature, primitive, decision, reason,
                       confidence, evidence_fingerprint) -> None:
        self.con.execute(
            "INSERT OR REPLACE INTO verdicts"
            "(signature,primitive,decision,reason,confidence,evidence_fingerprint,decided_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (sig_key(signature), primitive, decision, reason,
             None if confidence is None else float(confidence),
             evidence_fingerprint, _now_iso()))
        self.con.commit()

    def all_verdicts(self) -> dict:
        return {r["signature"]: dict(r) for r in self.con.execute("SELECT * FROM verdicts")}

    # -- meta --------------------------------------------------------------- #
    def get_meta(self, key: str, default=None):
        row = self.con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value) -> None:
        self.con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                         (key, str(value)))
        self.con.commit()

    def corpus_fingerprint(self, since_ms=None) -> str:
        """Hash of (path,size,mtime) over the in-window sessions + the window bound.
        An identical fingerprint means the in-window corpus is unchanged since a
        stored one. Exposed as a building block for callers that want to detect
        that; the mine pipeline does NOT currently use it to short-circuit the fold
        (re-run speed comes from skipping the parse of unchanged files)."""
        h = hashlib.sha1()
        if since_ms is None:
            cur = self.con.execute("SELECT path,size,mtime FROM sessions ORDER BY path")
        else:
            cur = self.con.execute(
                "SELECT path,size,mtime FROM sessions WHERE mtime >= ? ORDER BY path",
                (int(since_ms),))
        for p, s, m in cur:
            h.update(f"{p}|{s}|{m}\n".encode("utf-8"))
        h.update(f"since={since_ms}".encode("utf-8"))
        return h.hexdigest()


if __name__ == "__main__":  # tiny self-check
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        st = IndexStore.open(Path(d))
        assert st.classify("x.jsonl", 10, 1)[0] == "new"
        st.upsert_session("x.jsonl", 10, 1, 10, "h", {"file": "x.jsonl", "n_calls": 1})
        assert st.classify("x.jsonl", 10, 1) == ("unchanged", 10)
        assert st.classify("x.jsonl", 20, 2)[0] == "appended"
        assert st.classify("x.jsonl", 5, 2)[0] == "modified"
        assert st.count_sessions() == 1
        st.upsert_verdict(["Grep", "Read"], "skill", "keep", "recurs", 0.8, "fp1")
        assert st.get_verdict(["Grep", "Read"])["primitive"] == "skill"
        st.set_meta("last_run_epoch", 123)
        assert st.get_meta("last_run_epoch") == "123"
        st.close()
    print("index_store self-check OK")
