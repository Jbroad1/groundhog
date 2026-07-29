# Data formats

The exact shapes groundhog reads and writes. Field lists were verified against
real `~/.claude` data on Windows; parse defensively (many record types share a
file — always `.get()`).

## Inputs (read-only, under the resolved config dir)

### `history.jsonl` — one submitted prompt per line
```json
{"display": "<prompt text>", "pastedContents": {}, "timestamp": 1759138194478, "project": "C:\\path\\to\\repo"}
```
- `timestamp` is **epoch milliseconds** (int). `project` is an absolute path.
- No `sessionId` on these records in practice. Cheapest high-signal source
  (prompt clusters, slash-command frequency).

### `projects/<cwd-slug>/<uuid>.jsonl` — session transcripts
One file per session; **stream, never load whole** (files range ~130 B … 33 MB).
Records are mixed-type; the ones we mine:
- envelope fields (on `assistant`/`user`): `type`, `uuid`, `parentUuid`,
  `sessionId`, `timestamp` (**ISO-8601 string**), `cwd`, `gitBranch`.
- `assistant.message.content[]` blocks: `thinking` (empty — encrypted signature
  only, so reasoning is **inferred from the observable trace**, never read from
  here), `text`, and `tool_use` = `{id, name, input, ...}`.
- `user.message.content[]`: `tool_result` = `{tool_use_id, content, is_error?}`.
- top-level `toolUseResult` sometimes carries `{stdout, stderr, ...}`.
- other types we skip: `queue-operation`, `last-prompt`, `ai-title`, `attachment`.

### `plans/*.md` — green-lit multi-step plans
Markdown: `# <title>` then `## <section>` blocks; numbered/`Step` lines counted.

## Output: `scan.json` (deterministic, secret-scrubbed)

```json
{
  "schema_version": "1",
  "generated_at": "<iso>",
  "since_ms": 1750000000000,
  "config_dir": "C:\\Users\\...\\.claude",
  "env": { "allow_managed_hooks_only": true, "...": "..." },
  "sources": { "history": {...}, "transcripts": {...}, "plans": {...} },
  "existing_automations": { "skills": 42, "hooks": 3, "hookify_rules": 0, "skill_names": [...] },
  "candidate_count": 128,
  "candidates": [ Candidate, ... ]
}
```

### Candidate object
```json
{
  "id": "cand-0001",
  "kind": "tool-sequence",
  "title": "Grep -> Read -> Edit",
  "summary": "Tool sequence ... repeated across 7 sessions (19 occurrences).",
  "signature": ["Grep", "Read", "Edit"],
  "frequency": 7,
  "steps_saved": 3,
  "leverage": 21,
  "automatability": 0.7,
  "already_automated": 0.0,
  "score": 14.7,
  "recommended_primitive": "skill",
  "primitive_rationale": "A repeatable multi-step sequence ...",
  "managed_hook_warning": false,
  "projects": ["repo-a", "repo-b"],
  "proof_paths": [
    {"file": "C:\\...\\projects\\slug\\uuid.jsonl", "line": 123, "session": "<id>", "detail": "Grep -> Read -> Edit"}
  ],
  "evidence": { "occurrences": 19, "sessions": 7 }
}
```
`leverage = frequency × steps_saved` (the raw prize).
`score = frequency × steps_saved × automatability × (1 − already_automated)` (kept
as a field). Candidates are ordered **leverage-forward**: primary key is the
leverage rank (leverage, attenuated for harness/nav noise, suppressed when
already-automated), then `score`, then `signature` — so identical input ⇒
identical order, and the top-N slice `shard` takes is the real prize, not the
automatability-folded score. Harness/agent-plumbing and pure-navigation loops
carry `evidence.harness_noise` / `evidence.nav_noise` flags and are attenuated.

### Candidate kinds
| kind | source | provisional primitive |
|---|---|---|
| `tool-sequence` | frequent tool n-gram (len 2–5) across sessions | skill / skill-chain (len≥4 or spans Skill/Task) |
| `repeated-call-loop` | ≥3 identical consecutive calls | skill |
| `edit-hotspot` | a path edited ≥4 times | skill |
| `bash-template` | a command template run ≥4 times | hook/hookify if guard-ish (**structural**: recurs across ≥3 sessions, any domain; dev-CI verbs boost but don't gate), else skill |
| `error-fix` | `tool_result.is_error` → next tool_use, ≥3× | hook/hookify (guardrail) |
| `prompt-cluster` | ≥3 near-duplicate prompts (`history.jsonl`) | skill (incl. research/report requests; slash-command clusters flagged already-automated) |
| `plan-type` | ≥2 plans sharing a normalized title | skill-chain |

## Output: `env.json`
See `preflight.py`. Key fields: `platform`, `jq_available`,
`allow_managed_hooks_only`, `managed_policy_source`, `optional_skills{name→path|null}`,
`data{...counts}`, `warnings[]`.

## Input to `install`: install-plan JSON
Produced by the compile phase (LLM + writing-skills TDD); consumed by
`groundhog.py install`.
```json
{
  "artifacts": [
    {"type": "skill", "name": "review-pr-diff", "candidate_id": "cand-0003",
     "files": {"SKILL.md": "<...>", "scripts/helper.py": "<...>"}},
    {"type": "hookify-rule", "name": "run-tests-after-edit",
     "project": "C:\\path\\to\\repo", "content": "<hookify rule markdown>"},
    {"type": "hook", "name": "block-force-push",
     "settings_patch": {"hooks": {"PreToolUse": [ ... ]}}}
  ]
}
```
- `skill` files are written under `<config>/skills/<name>/` (paths sanitized:
  no absolute, no `..`), then re-validated.
- `hookify-rule` writes `<project>/.claude/hookify.<name>.local.md` (or the
  config dir if no project).
- `hook` merges into `<config>/settings.json` — **but is skipped with a warning
  when `allowManagedHooksOnly` is active** (it would be inert; emit a hookify
  rule instead).

## State: `<config>/groundhog/`
- `index.db` — the durable SQLite index (WAL). Supersedes `cache.json`; holds
  mined session data, so it is git-ignored. Tables:
  - `sessions(path PK, size, mtime, last_offset, prefix_hash, summary_json,
    last_scanned_at)` — one row per transcript. **Change detection is
    size-primary** (immune to OneDrive rewriting mtime): unknown path → new;
    `size==stored && mtime==stored` → skip; `size>stored` → append (resume from
    `last_offset`); `size==stored && mtime≠` → prefix-hash (first 4 KB + size)
    tiebreak; `size<stored` → modified. Per-session commit = crash-safe.
  - `aggregates(kind, signature, sessions, occurrences, steps_saved, proof_json)`
    — mine-layer rollup, materialised each run.
  - `verdicts(signature PK, primitive, decision, reason, confidence,
    evidence_fingerprint, decided_at)` — the verdict ledger. `groundhog remember`
    folds a `verdict.json` into it (merge, not overwrite); a later scan surfaces a
    candidate's `prior_verdict` when its signature + `evidence_fingerprint` match.
  - `meta(key, value)` — e.g. `last_run_epoch` (written only on success).
  - Corrupt db → renamed `index.db.bak` + rebuilt; a schema bump discards the
    index and forces one slow full rescan.
- `cache.json` — legacy per-file `{mtime, size, result}` cache (still used by the
  importable `scan_transcripts(..., cache=...)` path and `--no-cache` in-memory runs).
- `last-run/scan.json`, `last-run/env.json`, `shards/`, `finalists.json`.
- `manifest.json` — what was installed, for incremental re-runs and dedup.
