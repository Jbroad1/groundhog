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
`score = frequency × steps_saved × automatability × (1 − already_automated)`.
Candidates are sorted `(-score, signature)` — identical input ⇒ identical order.

### Candidate kinds
| kind | source | provisional primitive |
|---|---|---|
| `tool-sequence` | frequent tool n-gram (len 2–5) across sessions | skill / skill-chain (len≥4 or spans Skill/Task) |
| `repeated-call-loop` | ≥3 identical consecutive calls | skill |
| `edit-hotspot` | a path edited ≥4 times | skill |
| `bash-template` | a command template run ≥4 times | hook/hookify if guard-ish (test/lint/build…), else skill |
| `error-fix` | `tool_result.is_error` → next tool_use, ≥3× | hook/hookify (guardrail) |
| `prompt-cluster` | ≥3 near-duplicate prompts (`history.jsonl`) | skill (slash-command clusters flagged already-automated) |
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
- `cache.json` — per-file `{mtime, size, result}`; makes re-runs incremental.
- `last-run/scan.json`, `last-run/env.json`.
- `manifest.json` — what was installed, for incremental re-runs and dedup.
