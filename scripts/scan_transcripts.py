#!/usr/bin/env python3
"""
scan_transcripts.py -- extract ground-truth tool workflows from session
transcripts under ~/.claude/projects/<cwd-slug>/<uuid>.jsonl.

Each transcript file is one session. For every ``assistant`` message we read the
ordered ``tool_use`` blocks; from ``user`` messages we read ``tool_result``
errors. From that stream we derive, per session:

  * the ordered tool-name sequence (+ a parallel line-number array for proofs),
  * edit hotspots (Edit/Write/MultiEdit/NotebookEdit target paths),
  * Bash command templates,
  * error -> resolution pairs (an errored tool_result followed by the next tool_use).

Transcripts range from ~130 B to tens of MB, so files are streamed line-by-line
and never loaded whole.

Two incremental back-ends share one parser (``_Acc``):
  * **legacy** -- an ``(mtime, size)`` cache dict; unchanged files are not
    re-parsed. Kept for the importable API and the unit tests.
  * **index** -- the durable SQLite ``IndexStore`` (pass ``store=``). Adds a lazy
    ``os.scandir`` walk (never materialises the whole file list), size-primary
    change detection (OneDrive-safe), byte-offset resume for appended sessions,
    and a per-session commit so a crash leaves safe partial progress.

Importable (`scan_transcripts(...)`) and runnable standalone.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import groundhog_lib as fl
from index_store import head_hash

# Per-session caps keep memory and scan.json size bounded on pathological
# mega-sessions without losing the signal we mine on.
MAX_CALLS = 4000
MAX_EDITS = 2000
MAX_BASH = 2000
MAX_ERRFIX = 500
MAX_LINES = 60000   # hard per-file stop; transcripts can be tens of MB / 100k+ lines

_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def _is_error_result(block: dict) -> bool:
    if not isinstance(block, dict):
        return False
    return block.get("is_error") is True


class _Acc:
    """Accumulator for one session's mineable signal. Shared by the full parse and
    the append-resume parse so the two paths can never drift. Seed from ``base`` to
    continue an already-parsed session past a byte offset."""

    def __init__(self, path, project_dir=None, base=None):
        self.path = str(path)
        self.project_dir = project_dir or Path(path).parent.name
        b = base or {}
        self.session_id = b.get("session_id") or Path(path).stem
        self.cwd = b.get("cwd", "")
        self.git_branch = b.get("git_branch", "")
        self.first_ts = b.get("first_ts")
        self.last_ts = b.get("last_ts")
        self.sequence = list(b.get("sequence", []))
        self.lines = list(b.get("lines", []))
        self.edits = list(b.get("edits", []))
        self.bash = list(b.get("bash", []))
        self.error_fixes = list(b.get("error_fixes", []))
        self.n_lines = b.get("n_lines", 0)
        # id->tool and a pending error are per-turn state; not carried across an
        # append boundary (a pending error exactly at the split is dropped -- rare
        # and low-signal).
        self.id_to_tool: dict = {}
        self.pending_error_tool = None
        self.saturated = False

    def feed(self, rec, ln: int) -> None:
        if not isinstance(rec, dict):
            return
        rtype = rec.get("type")
        if not self.cwd and rec.get("cwd"):
            self.cwd = str(rec["cwd"])
        if not self.git_branch and rec.get("gitBranch"):
            self.git_branch = str(rec["gitBranch"])
        if rec.get("sessionId"):
            self.session_id = str(rec["sessionId"])
        ts = fl.to_epoch_ms(rec.get("timestamp"))
        if ts is not None:
            self.first_ts = ts if self.first_ts is None else min(self.first_ts, ts)
            self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)

        msg = rec.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            return

        if rtype == "assistant":
            for b in content:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                name = str(b.get("name", "?"))
                tid = b.get("id")
                tool_input = b.get("input", {})
                if tid:
                    self.id_to_tool[str(tid)] = name
                if self.pending_error_tool is not None:
                    if len(self.error_fixes) < MAX_ERRFIX:
                        self.error_fixes.append({"err": self.pending_error_tool,
                                                 "fix": name, "line": ln})
                    self.pending_error_tool = None
                if len(self.sequence) < MAX_CALLS:
                    self.sequence.append(name)
                    self.lines.append(ln)
                else:
                    self.saturated = True
                if name in _EDIT_TOOLS and len(self.edits) < MAX_EDITS:
                    fp = fl.file_path_from_input(name, tool_input)
                    if fp:
                        self.edits.append({"path": fl.repo_relative(fp, self.cwd), "line": ln})
                elif name == "Bash" and len(self.bash) < MAX_BASH:
                    cmd = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
                    if cmd:
                        self.bash.append({"tmpl": fl.template_bash(str(cmd)), "line": ln})
        elif rtype == "user":
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result" and _is_error_result(b):
                    tid = str(b.get("tool_use_id", ""))
                    self.pending_error_tool = self.id_to_tool.get(tid, "?")
                    break  # one error marker per user turn is enough

    def summary(self) -> dict:
        return {
            "file": self.path,
            "session_id": self.session_id,
            "project_dir": self.project_dir,
            "cwd": self.cwd,
            "git_branch": self.git_branch,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "n_calls": len(self.sequence),
            "n_lines": self.n_lines,
            "sequence": self.sequence,
            "lines": self.lines,
            "edits": self.edits,
            "bash": self.bash,
            "error_fixes": self.error_fixes,
        }


def extract_session(path: Path, project_dir: str | None = None) -> dict:
    """Full parse of a transcript (text-mode streaming). Legacy back-end + tests."""
    acc = _Acc(path, project_dir)
    for ln, rec in fl.iter_jsonl(path):
        if ln > MAX_LINES or acc.saturated:
            break
        acc.feed(rec, ln)
        acc.n_lines = ln
    return acc.summary()


def _read_jsonl_from(path, start_offset: int = 0, start_line: int = 0):
    """Yield (record, line_no, committed_offset) for each newline-terminated JSON
    line at/after ``start_offset``. ``committed_offset`` sits just past the line's
    newline, so resuming there never re-reads or splits a line. A trailing line
    with no newline is left for the next pass (it may still be mid-write)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(start_offset)
            offset = start_offset
            ln = start_line
            for raw in fh:                    # binary iteration splits on b"\n"
                if not raw.endswith(b"\n"):
                    break
                offset += len(raw)
                ln += 1
                s = raw.decode("utf-8", "replace").strip()
                if not s:
                    continue
                try:
                    yield json.loads(s), ln, offset
                except (ValueError, TypeError):
                    continue
    except (OSError, UnicodeError):
        return


def extract_or_extend(path, project_dir=None, start_offset: int = 0, base=None):
    """Parse a transcript from ``start_offset``, extending ``base`` if given.
    Returns (summary, end_offset). Full parse: start_offset=0, base=None."""
    acc = _Acc(path, project_dir, base=base)
    end_offset = start_offset
    for rec, ln, off in _read_jsonl_from(path, start_offset, acc.n_lines):
        if ln > MAX_LINES or acc.saturated:
            break
        acc.feed(rec, ln)
        acc.n_lines = ln
        end_offset = off
    return acc.summary(), end_offset


# --------------------------------------------------------------------------- #
def _iter_jsonl_entries(root: str):
    """Lazy recursive walk yielding ``os.DirEntry`` for every ``*.jsonl`` under
    ``root``. Uses ``os.scandir`` (stat cached on the entry) and never builds the
    whole file list in memory -- the rglob that crashed at ~1M transcripts."""
    stack = [str(root)]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.name.endswith(".jsonl") and entry.is_file(follow_symlinks=False):
                        yield entry
                except OSError:
                    continue


def _slug_for(root: Path, path: str) -> str:
    try:
        rel = Path(path).relative_to(root)
        return rel.parts[0] if rel.parts else Path(path).parent.name
    except ValueError:
        return Path(path).parent.name


def _scan_indexed(config_dir: Path, since_ms, store) -> dict:
    """Index back-end: incremental, crash-safe, bounded-memory. Updates the
    SQLite index during a lazy walk, then returns the in-window session summaries
    streamed back from the index (ordered by path -> deterministic)."""
    root = config_dir / "projects"
    out = {"source": "transcripts", "root": str(root), "exists": root.is_dir(),
           "files_total": 0, "files_in_window": 0, "files_parsed": 0,
           "cache_hits": 0, "sessions": []}
    if not root.is_dir():
        return out

    seen = []
    for entry in _iter_jsonl_entries(root):
        path = entry.path
        seen.append(path)
        try:
            st = entry.stat()
        except OSError:
            continue
        out["files_total"] += 1
        size = st.st_size
        mtime_ms = int(st.st_mtime * 1000)
        # Recency prefilter: a session last written before the window has no
        # in-window activity. Keep its row (it is in `seen`), just skip parsing.
        if since_ms is not None and mtime_ms < since_ms:
            continue
        out["files_in_window"] += 1

        state, resume = store.classify(path, size, mtime_ms)
        if state == "unchanged":
            out["cache_hits"] += 1
            continue
        slug = _slug_for(root, path)
        if state == "appended":
            row = store.get_session(path)
            base = None
            if row is not None:
                try:
                    base = json.loads(row["summary_json"])
                except (ValueError, TypeError):
                    base = None
            summary, end_off = extract_or_extend(path, slug, resume, base)
        else:  # new / modified -> full re-parse
            summary, end_off = extract_or_extend(path, slug, 0, None)

        # If we stopped at the per-file line cap there is nothing more to mine, so
        # point the resume offset at EOF to avoid re-reading a mega-session on
        # every future append.
        last_off = size if summary.get("n_calls", 0) >= MAX_CALLS else end_off
        store.upsert_session(path, size, mtime_ms, last_off,
                             head_hash(path, min(4096, size)), summary)  # per-session commit
        out["files_parsed"] += 1

    store.prune_missing(seen)
    out["sessions"] = list(store.iter_summaries(since_ms))
    return out


def scan_transcripts(config_dir: Path, since_ms: int | None = None,
                     cache: dict | None = None, store=None) -> dict:
    if store is not None:
        return _scan_indexed(config_dir, since_ms, store)

    root = config_dir / "projects"
    out = {"source": "transcripts", "root": str(root), "exists": root.is_dir(),
           "files_total": 0, "files_in_window": 0, "files_parsed": 0,
           "cache_hits": 0, "sessions": []}
    if not root.is_dir():
        return out
    cache = cache if cache is not None else {}

    # Recursive: session transcripts live at projects/<slug>/<uuid>.jsonl AND
    # subagent transcripts at projects/<slug>/<uuid>/subagents/agent-*.jsonl.
    # A one-level glob silently drops ~half the corpus (the subagent sessions,
    # which carry rich workflow signal). Enumerate in-process -- shelling out to
    # find/du over a OneDrive-synced tree times out (see references/red-baseline.md).
    files = sorted(root.rglob("*.jsonl"))
    out["files_total"] = len(files)
    for path in files:
        try:
            st = path.stat()
        except OSError:
            continue
        mtime_ms = int(st.st_mtime * 1000)
        # cheap recency prefilter: a session last written before the window
        # cannot contain in-window activity.
        if since_ms is not None and mtime_ms < since_ms:
            continue
        out["files_in_window"] += 1

        key = str(path)
        cached = cache.get(key)
        # Version-stamp the cache: a parser change (new SCHEMA_VERSION) must
        # invalidate old entries, or stale results silently reuse across upgrades.
        if (cached and cached.get("v") == fl.SCHEMA_VERSION
                and cached.get("mtime") == mtime_ms and cached.get("size") == st.st_size):
            out["sessions"].append(cached["result"])
            out["cache_hits"] += 1
            continue

        rel = path.relative_to(root).parts
        slug = rel[0] if rel else path.parent.name
        result = extract_session(path, project_dir=slug)
        out["files_parsed"] += 1
        cache[key] = {"v": fl.SCHEMA_VERSION, "mtime": mtime_ms,
                      "size": st.st_size, "result": result}
        out["sessions"].append(result)

    # Prune cache entries for transcripts that no longer exist (bounded growth).
    # Only touch transcript-shaped entries; out-of-window files are still in
    # `files`, so they are kept -- only truly-deleted files are dropped.
    valid_keys = {str(p) for p in files}
    for k in [k for k, v in cache.items()
              if isinstance(v, dict) and "result" in v and k not in valid_keys]:
        del cache[k]

    return out


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Scan session transcripts.")
    ap.add_argument("--config-dir")
    ap.add_argument("--since", default=fl.RECENCY_DEFAULT)
    args = ap.parse_args(argv)
    cfg = fl.resolve_config_dir(args.config_dir)
    res = scan_transcripts(cfg, fl.parse_since(args.since))
    print(f"transcripts: {res['files_in_window']}/{res['files_total']} in window, "
          f"{res['files_parsed']} parsed, {res['cache_hits']} cache hits")
    total_calls = sum(s["n_calls"] for s in res["sessions"])
    total_edits = sum(len(s["edits"]) for s in res["sessions"])
    total_err = sum(len(s["error_fixes"]) for s in res["sessions"])
    print(f"  tool calls: {total_calls}, edits: {total_edits}, error->fix pairs: {total_err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
