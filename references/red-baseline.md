# RED baseline — watching it fail without groundhog

Per the writing-skills Iron Law ("NO SKILL WITHOUT A FAILING TEST FIRST"), a fresh
Claude Code agent was given the raw task **without** groundhog, read-only, on
this real machine:

> "Look at my ~/.claude history and turn my repeated workflows into skills and
> hooks, so next time the automation already exists."

## What actually happened (verbatim failure modes)

- **`jq` FAILED.** `command -v jq && jq --version` → `JQ_NOT_FOUND (exit 1)`. No
  JSON tooling; the agent hand-rolled with Grep/Read/Glob.
- **Directory traversal choked before any transcript was read.** `du -sm projects`
  and `find projects -name '*.jsonl' -printf '%s\t%p\n'` **both exceeded the 120s
  timeout** (the `projects\` tree is OneDrive-synced + AV-scanned). The agent
  never opened a single transcript.
- **The mega-transcripts are real.** A backgrounded `find` later returned sizes:
  a **33,117,573-byte (33MB)** single session (`AI-workbench/...jsonl`), plus
  25.9 / 25.3 / 23.1 / 22.1 MB sessions, a **17.6MB subagent** transcript, and a
  dozen more 9–16MB. "Any approach that reads a whole transcript … will fail or
  wreck the context window."
- **Glob was capped:** "Showing 100 of 2866 matching files; 2766 more are not
  listed." (Note: 2866 includes nested `…/<uuid>/subagents/agent-*.jsonl`.)
- **Permission friction:** 2 PowerShell probes were DENIED by the classifier;
  ~4 of ~13 tool calls were burned on denials + timeouts before reading a prompt.
- **Dedup: effectively NO.** Name/description scan only across ~316 installed
  skills; no semantic comparison. It could *see* that most patterns were already
  covered (planning→`blueprint`/`writing-plans`; git push→`git-workflow`; verify
  →`verification-loop`) but did not perform a real dedup pass.
- **Secret redaction: NONE.** Zero redaction; `history.jsonl` leaks absolute
  paths with the username; real secrets live in the transcripts it never scanned.
- **`allowManagedHooksOnly`: unverified.** It checked `C:\ProgramData\ClaudeCode\
  managed-settings.json` and `~/.claude/managed-settings.json` — and **missed
  `~/.claude/remote-settings.json`**, which is exactly where the policy is set.
  So any generated hook could be silently inert.
- **Net:** only prompt-string patterns from `history.jsonl` (a fraction of the
  corpus) were salvaged; the **2866 transcripts — the real workflow signal — went
  completely untouched.** "Not partial success."

## RED → GREEN: each failure maps to a component

| Baseline failure | groundhog component |
|---|---|
| `jq` missing | Pure-Python engine; no `jq`, no shell JSON tooling. |
| `find`/`du` traversal timeout | In-process enumeration (`rglob`) + `mtime` recency prefilter; never shells out to walk the tree. |
| Never streamed; 33MB would blow context | `iter_jsonl` streams line-by-line; per-session caps + `MAX_LINES` early-exit; never reads a whole transcript into a model. |
| Glob capped at 100/2866 | Enumerate **all** files in Python, including nested `subagents/*.jsonl`. |
| No dedup vs 316 skills | `inventory_automations` + `already_automated` scoring + analyzer semantic dedup. |
| No redaction | `scrub.py` runs on every extracted string before it reaches `scan.json`. |
| Managed policy missed (`remote-settings.json`) | `preflight.py` reads settings/managed/remote; `allow_managed_hooks_only` threads into primitive selection; install refuses inert hooks. |
| Only mined `history.jsonl` | Mines history **and** transcripts **and** plans; tool-sequence/loop/error-fix signal from the transcripts. |
| Permission friction from ad-hoc probes | One vetted set of Python scripts instead of exploratory shell probes. |

The GREEN run (WITH groundhog) is the same scenario succeeding end-to-end; see
the `red_green_gate` in [../evals/evals.json](../evals/evals.json).
