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
and never loaded whole. An (mtime, size) cache makes re-runs incremental: an
unchanged file is not re-parsed.

Importable (`scan_transcripts(...)`) and runnable standalone.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import groundhog_lib as fl

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
    if block.get("is_error") is True:
        return True
    return False


def extract_session(path: Path, project_dir: str | None = None) -> dict:
    """Parse a single transcript into a compact, cacheable summary."""
    session_id = path.stem
    cwd = ""
    git_branch = ""
    first_ts = None
    last_ts = None

    sequence: list[str] = []
    lines: list[int] = []
    edits: list[dict] = []
    bash: list[dict] = []
    error_fixes: list[dict] = []

    id_to_tool: dict[str, str] = {}
    pending_error_tool: str | None = None  # set when an error result seen; paired with next tool_use

    saturated = False
    for ln, rec in fl.iter_jsonl(path):
        if ln > MAX_LINES:
            break
        # once every collector is full, the rest of a mega-transcript adds
        # nothing we mine -- stop early (huge speedup on 20-33MB sessions).
        if saturated:
            break
        if not isinstance(rec, dict):
            continue
        rtype = rec.get("type")

        # envelope metadata (present on assistant/user/attachment records)
        if not cwd and rec.get("cwd"):
            cwd = str(rec["cwd"])
        if not git_branch and rec.get("gitBranch"):
            git_branch = str(rec["gitBranch"])
        if rec.get("sessionId"):
            session_id = str(rec["sessionId"])
        ts = fl.to_epoch_ms(rec.get("timestamp"))
        if ts is not None:
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)

        msg = rec.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue

        if rtype == "assistant":
            for b in content:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                name = str(b.get("name", "?"))
                tid = b.get("id")
                tool_input = b.get("input", {})
                if tid:
                    id_to_tool[str(tid)] = name

                # pair a preceding error with this resolution step
                if pending_error_tool is not None:
                    if len(error_fixes) < MAX_ERRFIX:
                        error_fixes.append({"err": pending_error_tool, "fix": name, "line": ln})
                    pending_error_tool = None

                if len(sequence) < MAX_CALLS:
                    sequence.append(name)
                    lines.append(ln)
                else:
                    saturated = True  # 4000 calls is ample; stop scanning this mega-session

                if name in _EDIT_TOOLS and len(edits) < MAX_EDITS:
                    fp = fl.file_path_from_input(name, tool_input)
                    if fp:
                        edits.append({"path": fl.repo_relative(fp, cwd), "line": ln})
                elif name == "Bash" and len(bash) < MAX_BASH:
                    cmd = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
                    if cmd:
                        bash.append({"tmpl": fl.template_bash(str(cmd)), "line": ln})

        elif rtype == "user":
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result" and _is_error_result(b):
                    tid = str(b.get("tool_use_id", ""))
                    pending_error_tool = id_to_tool.get(tid, "?")
                    break  # one error marker per user turn is enough

    return {
        "file": str(path),
        "session_id": session_id,
        "project_dir": project_dir or path.parent.name,
        "cwd": cwd,
        "git_branch": git_branch,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "n_calls": len(sequence),
        "sequence": sequence,
        "lines": lines,
        "edits": edits,
        "bash": bash,
        "error_fixes": error_fixes,
    }


def scan_transcripts(config_dir: Path, since_ms: int | None = None,
                     cache: dict | None = None) -> dict:
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
