#!/usr/bin/env python3
"""
make_fixtures.py -- build a deterministic fake ~/.claude tree for tests and for
the RED/GREEN demo. Everything here is synthetic; no real user data.

The tree is designed so the miner MUST find, deterministically:
  * a tool-sequence  Grep -> Read -> Edit  (in 4 sessions),
  * an edit-hotspot  src/app.py            (4 edits),
  * a recurring Bash guardrail  `pytest tests/ -q`  (4 sessions),
  * an error->fix pair  Bash error -> Edit  (3 sessions),
  * a repeated-call loop  Read x4           (1 session),
  * a prompt-cluster carrying a PLANTED SECRET that must be scrubbed,
  * a slash-command cluster that must be flagged already-automated,
  * a plan-type cluster (2 identical plan shapes),
and that an existing skill (`pytest-runner`) raises `already_automated` on the
pytest candidate (dedup).
"""
from __future__ import annotations

import json
from pathlib import Path

CWD_A = "C:/work/proj-a"
PLANTED_SECRET = "ghp_EXAMPLEONLYNOTAREALTOKEN00"  # obviously-synthetic GitHub-token shape


def _asst(session, ts, tid, name, tool_input, cwd=CWD_A):
    return {"type": "assistant", "uuid": f"{session}-{tid}", "parentUuid": None,
            "sessionId": session, "timestamp": ts, "cwd": cwd, "gitBranch": "main",
            "message": {"role": "assistant",
                        "content": [{"type": "tool_use", "id": tid, "name": name,
                                     "input": tool_input}]}}


def _result(session, ts, tid, cwd=CWD_A, is_error=False, content="ok"):
    block = {"type": "tool_result", "tool_use_id": tid, "content": content}
    if is_error:
        block["is_error"] = True
    return {"type": "user", "sessionId": session, "timestamp": ts, "cwd": cwd,
            "message": {"role": "user", "content": [block]}}


def _write_jsonl(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def build(dest: Path) -> Path:
    """Construct the fixture config dir at `dest` and return it."""
    dest = Path(dest)
    proj = dest / "projects" / "C--work-proj-a"
    ts = "2026-07-20T10:00:00.000Z"

    # --- Group A: Grep -> Read -> Edit(src/app.py) in s1..s4 ---------------- #
    for i in range(1, 5):
        s = f"seqA{i}"
        recs = [
            _asst(s, ts, "g", "Grep", {"pattern": "TODO", "output_mode": "content"}),
            _result(s, ts, "g"),
            _asst(s, ts, "r", "Read", {"file_path": f"{CWD_A}/src/app.py"}),
            _result(s, ts, "r"),
            _asst(s, ts, "e", "Edit", {"file_path": f"{CWD_A}/src/app.py",
                                       "old_string": "a", "new_string": "b"}),
            _result(s, ts, "e"),
        ]
        _write_jsonl(proj / f"{s}.jsonl", recs)

    # --- Group B: recurring pytest + error->fix in s5..s7, plain in s8 ------ #
    for i in range(5, 8):
        s = f"seqB{i}"
        recs = [
            _asst(s, ts, "b", "Bash", {"command": "pytest tests/ -q"}),
            _result(s, ts, "b", is_error=True, content="1 failed"),
            _asst(s, ts, "e", "Edit", {"file_path": f"{CWD_A}/src/fix.py",
                                       "old_string": "x", "new_string": "y"}),
            _result(s, ts, "e"),
        ]
        _write_jsonl(proj / f"{s}.jsonl", recs)
    _write_jsonl(proj / "seqB8.jsonl", [
        _asst("seqB8", ts, "b", "Bash", {"command": "pytest tests/ -q"}),
        _result("seqB8", ts, "b", content="passed"),
    ])

    # --- Group C: a Read x4 loop in one session ----------------------------- #
    loop = []
    for k in range(4):
        loop.append(_asst("loop1", ts, f"rd{k}", "Read", {"file_path": f"{CWD_A}/src/mod{k}.py"}))
        loop.append(_result("loop1", ts, f"rd{k}"))
    _write_jsonl(proj / "loop1.jsonl", loop)

    # --- history.jsonl: secret cluster + slash cluster ---------------------- #
    epoch = 1_753_600_000_000  # arbitrary fixed epoch-ms within any recent window
    hist = []
    for _ in range(3):
        hist.append({"display": f"please deploy the app with token={PLANTED_SECRET}",
                     "pastedContents": {}, "timestamp": epoch, "project": CWD_A})
    for _ in range(3):
        hist.append({"display": "/deploy", "pastedContents": {}, "timestamp": epoch,
                     "project": CWD_A})
    _write_jsonl(dest / "history.jsonl", hist)

    # --- plans: two identical shapes ---------------------------------------- #
    plan_md = ("# Refactor the auth module\n\n## Context\nThe auth module is messy.\n\n"
               "## Steps\n1. Extract helpers\n2. Add tests\n3. Wire it up\n")
    (dest / "plans").mkdir(parents=True, exist_ok=True)
    (dest / "plans" / "plan-001.md").write_text(plan_md, encoding="utf-8")
    (dest / "plans" / "plan-002.md").write_text(plan_md, encoding="utf-8")

    # --- existing skill for dedup ------------------------------------------- #
    sk = dest / "skills" / "pytest-runner"
    sk.mkdir(parents=True, exist_ok=True)
    (sk / "SKILL.md").write_text(
        "---\nname: pytest-runner\n"
        "description: Use when running the pytest test suite after edits.\n---\n\n"
        "# pytest-runner\nRuns pytest.\n", encoding="utf-8")

    # --- settings: an existing hook + managed policy ------------------------ #
    (dest / "settings.json").write_text(json.dumps({
        "hooks": {"PostToolUse": [{"matcher": "Edit",
                                   "hooks": [{"type": "command", "command": "echo edited"}]}]}
    }, indent=2), encoding="utf-8")
    (dest / "remote-settings.json").write_text(json.dumps({
        "allowManagedHooksOnly": True,
        "hooks": {}
    }, indent=2), encoding="utf-8")

    return dest


if __name__ == "__main__":
    import sys
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("_fixtures")
    build(out)
    print(f"fixtures written to {out.resolve()}")
