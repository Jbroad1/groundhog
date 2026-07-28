# Primitive selection — the "compiler"

Each approved candidate compiles into exactly one automation primitive. The
deterministic miner assigns a *provisional* primitive; the analyzer subagent
confirms or overrides it using the table below and the candidate's evidence.

## Decision table

| Signal in the evidence | Primitive | Why |
|---|---|---|
| A deterministic check that must fire on a tool event (lint/test/format/build after edits; a rule that should always run) | **hook** — or **hookify rule** if `allowManagedHooksOnly` is active | Guardrails belong on the event, not in the model's memory. See [managed-hooks.md](managed-hooks.md). |
| A recurring error → resolution pair (the same mistake, then the same fix) | **hook / hookify rule** | Catch the mistake *before* it happens. |
| A repeatable multi-step task that needs judgment (a tool sequence, an edit hotspot, a recurring request) | **skill** | Judgment can't be hard-coded; a skill captures the steps + triggers. |
| A recurring pipeline that spans multiple skills/steps (a repeating *plan shape*, or a sequence that already invokes `Skill`/`Task`) | **skill-chain** | One skill that invokes sub-skills in order and documents the chain. |

## Rules of thumb

- **Prefer a skill over a hook** unless the behavior is a *deterministic guardrail
  that must fire every time*. Hooks are for enforcement; skills are for judgment.
  (writing-skills: "Mechanical constraints → automate; judgment calls → document.")
- **Prefer a hookify rule over a settings hook** whenever `allowManagedHooksOnly`
  is set — a settings/plugin hook may be silently inert there.
- **A single primitive per candidate.** If a candidate really needs two (e.g. a
  skill *and* a guardrail hook), split it into two candidates.
- **Never re-automate.** If `already_automated` is high or the analyzer finds a
  matching existing skill/hook, drop the candidate or propose an *edit* to the
  existing one instead.

## What "needs judgment" looks like

- Variable inputs, branching decisions, reading context before acting → **skill**.
- Fixed trigger, fixed action, pass/fail → **hook**.
- Example: "after editing any `*.py`, run the test suite" is a **hook/hookify
  rule** (deterministic). "Investigate a failing test and propose a fix" is a
  **skill** (judgment).

## Authoring each primitive

Every emitted primitive is authored with the writing-skills **RED → GREEN →
REFACTOR** cycle (see [authoring-method.md](authoring-method.md)) and validated
before install:
- skills → `scripts/validate_skill.py`
- hooks / hookify rules → `scripts/validate_hook.py`

Concrete skeletons for each primitive live in
[primitive-templates.md](primitive-templates.md).
