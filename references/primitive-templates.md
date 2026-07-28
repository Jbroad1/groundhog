# Primitive templates (the compile phase)

Skeletons for each primitive groundhog emits. The compile phase fills these in
via writing-skills TDD (see [authoring-method.md](authoring-method.md)) and wraps
them in an install-plan for `groundhog.py install`.

Validate before install:
- skill → `python scripts/validate_skill.py <dir>`
- settings hook → `python scripts/validate_hook.py <hook.json>`
- hookify rule → `python scripts/validate_hook.py <rule.md>`

---

## 1. Skill

`skills/<name>/SKILL.md`:
```markdown
---
name: <verb-first-name>
description: Use when <specific triggering condition observed in the proof paths>
---

# <Name>

## Overview
<Core purpose in 1–2 sentences.>

## When to use
- <symptom / situation 1>
- <symptom / situation 2>
When NOT to use: <...>

## Steps
1. <step grounded in the mined tool sequence>
2. ...

## Example
<one concrete, runnable example>
```
Heavy reference or reusable code → separate files in the skill dir.

---

## 2. Hookify rule (runtime-read — safe under `allowManagedHooksOnly`)

`.claude/hookify.<name>.local.md`. Events: `bash | file | stop | prompt | all`.
Name verb-first: `warn-*`, `block-*`, `require-*`.

Single pattern:
```markdown
---
name: require-tests-after-py-edit
enabled: true
event: file
action: warn
pattern: \.py$
---
You edited a Python file. Run the test suite before moving on (mined: edits to
`*.py` are repeatedly followed by `pytest`).
```

Multiple conditions:
```markdown
---
name: warn-env-api-keys
enabled: true
event: file
action: warn
conditions:
  - field: file_path
    operator: regex_match
    pattern: \.env$
  - field: new_text
    operator: contains
    pattern: API_KEY
---
Adding an API key to a .env file — make sure it is gitignored.
```
Condition fields: bash→`command`; file→`file_path`/`new_text`/`old_text`/`content`;
prompt→`user_prompt`. Operators: `regex_match`, `contains`, `equals`,
`not_contains`, `starts_with`, `ends_with`.

---

## 3. Settings hook (only when `allowManagedHooksOnly` is NOT active)

`hook.json` (merged into `settings.json`):
```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          {"type": "command", "command": "python -m pytest -q", "timeout": 120}
        ]
      }
    ]
  }
}
```
Events: PreToolUse, PostToolUse, UserPromptSubmit, Notification, Stop,
SubagentStop, PreCompact, SessionStart, SessionEnd. `matcher` is a tool-name
regex (PreToolUse/PostToolUse only).

---

## 4. Skill-chain (a skill that sequences sub-skills)

For a recurring multi-step *plan shape*:
```markdown
---
name: <pipeline-name>
description: Use when <the recurring multi-step situation from plan-type evidence>
---

# <Pipeline Name>

Runs these sub-skills in order:
1. **<skill-a>** — <what/why>
2. **<skill-b>** — <what/why>
3. **<skill-c>** — <what/why>

## Chain
For each step, invoke the sub-skill, confirm its output, then proceed. Stop and
report if a step fails. Document inputs/outputs passed between steps here.
```
Prefer referencing existing skills by name (don't duplicate them).

---

## 5. Install-plan wrapper

Wrap the artifacts above and hand the plan to `groundhog.py install --plan plan.json`
(preview) then `--yes` (write). The install-plan schema — the `artifacts` array
and each artifact type's fields — is defined once in
[data-formats.md](data-formats.md) (see *Input to `install`: install-plan JSON*);
follow that shape rather than duplicating it here.
