---
name: groundhog
description: Use when a user wants to turn multi-step processes they repeat with Claude Code — month-end reporting, competitor teardowns, client onboarding, RFP responses, research-to-report workflows, and developer workflows too — into skills, hooks, or skill-chains by mining their local ~/.claude history, or to retroactively build automations from past sessions. Not for authoring a single known skill or hook from scratch.
license: MIT
metadata:
  author: Joshua Broad
  version: 1.0.0
---

# groundhog

## Overview

Reverse-engineer what you actually do with Claude Code from your own local
session data: spot the tasks you repeat — a weekly market brief, a competitor
scan, a recurring report, and (for developers) tool sequences and guardrails —
and compile each into the right automation (**skill**, **hook**, or
**skill-chain**). Evidence-first, secret-scrubbed (best-effort — review
`scan.json` before sharing), review-by-default.

## When to use

- "Turn the things I keep asking Claude to do into reusable skills." "What do I
  keep doing every week? Automate it." — research, reports, competitor/market
  scans, data pulls, and drafting, as well as developer workflows.
- Retroactively mining `~/.claude` history/transcripts/plans for automatable patterns.

**Not** for authoring one specific skill from scratch (use writing-skills /
skill-creator) or writing one known hook (write it directly).

## Workflow

Run the bundled scripts — **never hand-roll this.** They exist because the naive
approach fails here: no `jq`, `find`/`du` over the OneDrive-synced `projects\`
tree times out, transcripts reach 33MB, and secrets/managed-hooks get missed
(see [references/red-baseline.md](references/red-baseline.md)).

1. **Preflight.** `python scripts/preflight.py` — resolves the config dir,
   detects `jq`, the `allowManagedHooksOnly` policy, and optional skills.
2. **Scan.** `python scripts/groundhog.py scan --since 30d` — streams history +
   transcripts (incl. nested `subagents/*.jsonl`) + plans into a scored,
   secret-scrubbed `scan.json` with proof-paths. First run is slow (cold cache);
   re-runs are incremental. Widen with `--since 90d` / `all`.
3. **Analyze.** Dispatch the **analyzer** subagent ([agents/analyzer.md](agents/analyzer.md)),
   passing it the groundhog directory and the `scan.json` path: it clusters,
   **dedups against skills you already have**,
   confirms a primitive per candidate, and writes a ranked `verdict.json`. Drop
   exploration noise; keep proof-backed proposals.
4. **Propose.** Present the ranked proposals (title, primitive, leverage, one
   proof-path each) with `AskUserQuestion`; let the user pick which to build.
   `--yes` auto-selects the top-N above threshold for unattended runs.
5. **Compile.** Author each approved primitive via writing-skills TDD (RED→GREEN→
   REFACTOR — [references/authoring-method.md](references/authoring-method.md))
   using [references/primitive-templates.md](references/primitive-templates.md).
   Assemble an install-plan JSON.
6. **QA + install.** `python scripts/groundhog.py install --plan plan.json` previews
   (no writes); re-run with `--yes` to apply. Emitted skills/hooks are validated;
   inert hooks are refused under managed policy.

## Guardrails

- **Never read a whole transcript into context** (they reach 33MB). The scripts
  stream and cap; you call the scripts, you don't `cat`/Read transcripts.
- **Never write an automation without approval.** `scan` is read-only; `install`
  previews unless `--yes`. Default path = the user picked it.
- **Never re-automate.** If a candidate matches an existing skill/hook, drop it or
  propose an edit — don't rebuild it.
- **Respect `allowManagedHooksOnly`.** When active, emit a hookify rule, not an
  inert settings hook ([references/managed-hooks.md](references/managed-hooks.md)).
- **Proof or it didn't happen.** Every proposal carries a real `proof_path`.

## Primitive choice

hook/hookify = deterministic guardrail that must fire on an event; skill =
repeatable task needing judgment; skill-chain = a recurring multi-step pipeline.
Full table: [references/primitive-selection.md](references/primitive-selection.md).
