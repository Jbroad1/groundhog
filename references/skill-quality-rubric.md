# Skill quality rubric — the reviewer's grader

The reviewer uses this when no installed QA skill (`skill-comply`, `skill-stocktake`)
is present. It is the bar a compiled primitive must clear before install. The
reviewer grades an **independent** draft — never one it authored.

Reason first, then score. Write the one-line finding before the number.

## The bars

Score each 1–5 against these anchors.

### Triggerability — will the right agent load this at the right time?
The `description` is the only text an agent sees before deciding to use the skill.
- **1** — vague or first-person; won't discriminate ("Helps with tasks").
- **3** — starts with "Use when", third-person, roughly on-target.
- **5** — "Use when …", third-person, names the concrete triggering situation and
  implies when NOT to use it. Describes the trigger only — **not the workflow**.
  A description that summarises the steps becomes a shortcut agents follow instead
  of reading the body; that caps this axis at 2.

### Grounding — is every claim traceable to the evidence?
- **1** — invents steps, tools, or facts not in the proof.
- **3** — mostly grounded, a little speculative padding.
- **5** — every step traces to the mined proof paths; no invented behaviour.
  For a guardrail: it states only what the proof shows and asserts **no**
  environment fact (an exit code, "command fails") absent from the proof
  (the head/tail bar — see [scoring-rubric.md](scoring-rubric.md)).

### Shape — is it lean and well-formed?
- **1** — missing frontmatter or H1; invalid `name`; frontmatter over 1024 chars.
- **3** — valid, but bloated body or vague steps.
- **5** — valid frontmatter (`validate_skill.py` clean), body under ~500 words,
  one strong concrete example, heavy detail pushed to reference files.

### Fit — is this the right primitive, and does it avoid duplication?
- **1** — wrong primitive (a judgment task forced into a hook), or duplicates an
  existing skill.
- **3** — defensible primitive, minor overlap.
- **5** — primitive matches [primitive-selection.md](primitive-selection.md); does
  not rebuild anything the user already has.

## Passing

- **Pass** requires every axis ≥ 3 **and** Triggerability + Grounding both ≥ 4.
- Emit `confidence` (0–1). Below 0.7, treat as a fail and return fixes.
- On a fail, return **specific, actionable fixes** ("description summarises the
  workflow — cut to the trigger", "step 3 cites no proof"), not a vibe. The author
  revises and resubmits; re-review until it passes or you have looped 3 times
  (then report the blocker to the orchestrator, do not install).

## Hard-negative reference

A draft whose description reads "Use when reviewing PRs — greps the diff, runs the
tests, then posts comments" **fails**: Triggerability ≤ 2 (it summarises the
workflow). The fix is "Use when reviewing a pull-request diff before approving."
A draft that invents "the Bash tool errors on `head`/`tail`" with no proof
**fails** Grounding. Both must be caught before install.
