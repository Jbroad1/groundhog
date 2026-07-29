---
name: groundhog
description: Use when a user wants to turn multi-step processes they repeat with Claude Code into skills, hooks, or skill-chains by mining their local ~/.claude history, or to retroactively build automations from past sessions. Covers recurring business work (month-end reporting, competitor teardowns, client onboarding, RFP responses, research-to-report) as well as developer workflows. Not for authoring a single known skill or hook from scratch.
license: MIT
metadata:
  author: Joshua Broad
  version: 1.0.0
---

# groundhog

## Overview

groundhog finds the work you repeat with Claude Code and turns it into automation.

It reads your own local session history and surfaces the tasks you do over and
over: a weekly market brief, a competitor scan, a recurring report, and for
developers, tool sequences and guardrails. Each pattern compiles into the right
automation — a **skill**, a **hook**, or a **skill-chain**.

Two promises hold throughout. Evidence-first: every proposal points to a real
session you can open. Review-by-default: scanning only reads, secrets are scrubbed
(best-effort, so check `scan.json` before you share it), and nothing installs
without your approval.

## When to use

- "Turn the things I keep asking Claude to do into reusable skills." "What do I
  keep doing every week? Automate it." — research, reports, competitor/market
  scans, data pulls, and drafting, as well as developer workflows.
- Retroactively mining `~/.claude` history/transcripts/plans for automatable patterns.

**Not** for authoring one specific skill from scratch (use writing-skills /
skill-creator) or writing one known hook (write it directly).

## Voice

Everything groundhog shows the user is written sharp and professional. This is the
enforceable register. Apply it to every proposal and summary.

- **Lead with the insight, in plain English.** Say what the pattern is and why it
  helps before naming any mechanism.
- **Demote jargon to a footnote.** Tool names, event types, matchers, exit codes
  go in a trailing `[technical: …]` line, never the lead.
- **One idea per line.** No em-dash chains, no stacked parentheticals.
- **Write for a smart non-coder.** A month-end analyst should understand the
  proposal even if the automation happens to be a developer hook.

## Workflow

Run the bundled scripts. Never hand-roll this.

The naive approach fails on real data, which is why the scripts exist — a fresh
agent without them stalls before it opens a single transcript.
[technical: no `jq`; `find`/`du` over the OneDrive-synced `projects\` tree times
out; transcripts reach 33 MB; secrets and the managed-hooks policy get missed. See
[references/red-baseline.md](references/red-baseline.md).]

1. **Preflight.** `python scripts/groundhog.py preflight` — check the ground before
   mining. It resolves the config dir, flags what changes the plan (the managed-hooks
   policy, whether `jq` is around, which optional skills you have), and writes
   `env.json` to the run dir — the file the analyzer workers read in step 3.
2. **Scan.** `python scripts/groundhog.py scan --since 30d` — read the history into
   one scored, secret-scrubbed `scan.json`, every candidate carrying a proof-path.
   The first run is slow (cold cache); re-runs are incremental. Widen the window
   with `--since 90d` or `all`.
   [technical: streams `history.jsonl` + session transcripts incl. nested
   `subagents/*.jsonl` + approved plans.]
3. **Analyze — split the work across parallel workers, then merge.** One agent
   reading the whole scan is slow. Fan it out instead:
   1. **Shard.** `python scripts/groundhog.py shard --scan <scan.json> --n 5 --top 500`
      splits the top leverage-ranked candidates into `shards/shard-NN.json`, striped
      so each worker sees a representative cross-section (not a block of near-equal scores).
   2. **Fan out.** Spawn one **analyzer** subagent per shard
      ([agents/analyzer.md](agents/analyzer.md)) **in a single message** so they run
      in parallel. Give each its shard path, [references/scoring-rubric.md](references/scoring-rubric.md),
      and `env.json`. Each scores against the rubric's absolute anchors, dedups
      against skills you already have, and writes `verdict-shard-NN.json`.
   3. **Merge.** `python scripts/groundhog.py merge --shards <shards_dir>` collapses
      cross-shard duplicates on `dedup_key`, ranks by score, and emits ~15–20
      `finalists.json`.
   4. **Re-score and cap.** In this main context, re-score the finalists in one pass
      against the same rubric for global calibration, keep the top 5–8, and write the
      single `verdict.json` that Propose reads.

   Defaults: 5 workers (raise to 8 if you hit no rate limits), 50–200 candidates per
   shard. If a worker times out, `merge` proceeds on the shards that finished and
   notes which one stalled.
4. **Propose.** Show each proposal as a plain-English card, then let the user pick
   with `AskUserQuestion`. Use the `plain_summary` and `technical_footnote` the
   analyzer produced. One card per proposal:

   ```
   Shell-nativism guard · hook · seen in 55 sessions
   Warn before a Bash step assumes a Unix shell that isn't there.
   Proof: 55 sessions · Confidence: 0.8
   [technical: PreToolUse hook on Bash; flags `head`/`tail`/`grep` pipelines]
   ```

   Header carries the name, the primitive, and the count. The line under it says
   what it does and why, in plain English. Mechanism goes in `[technical: …]`.
   `--yes` auto-selects the top proposals above threshold for unattended runs.
5. **Compile — author, then review, then revise.** For each approved proposal, run
   a two-agent loop so no draft grades its own homework:
   1. **Author** ([agents/compiler.md](agents/compiler.md)) drafts the primitive
      test-first, using the best authoring skill installed (`skill-creator` /
      `writing-skills`) or the bundled method if none.
   2. **Review** ([agents/reviewer.md](agents/reviewer.md)) grades it independently
      against `skill-comply` / `skill-stocktake` if installed, else
      [references/skill-quality-rubric.md](references/skill-quality-rubric.md).
   3. **Revise** until it clears the bar, then assemble the install-plan JSON.

   You (the orchestrator) hold the loop; author and reviewer run on Sonnet. With
   none of those skills installed it degrades to the bundled authoring method plus
   the bundled rubric. [technical: writing-skills TDD (RED→GREEN→REFACTOR),
   see [references/authoring-method.md](references/authoring-method.md) and
   [references/primitive-templates.md](references/primitive-templates.md).]
6. **Install.** `python scripts/groundhog.py install --plan plan.json` previews the
   writes; add `--yes` to apply. Every emitted skill and hook is validated first,
   and a hook that would sit there inert is refused under the managed-hooks policy.

## Guardrails

- **Don't read whole transcripts.** They reach 33 MB and will wreck your context.
  The scripts stream and cap — call them, never `cat` or Read a transcript.
- **Don't install without approval.** Scanning only reads. Install previews unless
  you pass `--yes`. The user picks what gets built.
- **Don't rebuild what exists.** If a candidate matches a skill or hook the user
  already has, drop it or propose an edit.
- **Honour `allowManagedHooksOnly`.** When it is set, emit a hookify rule instead
  of a settings hook that would sit there inert
  ([references/managed-hooks.md](references/managed-hooks.md)).
- **No proof, no proposal.** Every proposal carries a real `proof_path`.

## Primitive choice

A **hookify rule** is groundhog's own term for a runtime-read policy file that
still fires when a managed policy has disabled settings-level hooks.

- **hook** (or **hookify rule** under managed policy) — a deterministic guardrail
  that must fire on an event.
- **skill** — a repeatable task that needs judgment.
- **skill-chain** — a recurring multi-step pipeline.

Full table: [references/primitive-selection.md](references/primitive-selection.md).
