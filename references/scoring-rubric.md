# Scoring rubric — the shared spec every analyzer worker uses

This rubric is the grader. Every analyzer worker scores its shard against **these
absolute anchors**, never against the other candidates in its own shard. That is
what makes scores from five different workers comparable when `merge` ranks them
together. Score the candidate in front of you against the anchors below — do not
grade on a curve.

Reason first, then score (write the one-line justification before the number).

## The four axes

Give each axis an integer 1–5 using these anchors.

### Leverage — how much manual work automating this removes
`leverage = frequency × steps_saved` is the starting evidence; judge the real prize.
- **1** — rare or trivial: seen in fewer than 3 sessions, or saves ~1 step.
- **3** — recurs across several sessions and removes a few manual steps.
- **5** — recurs across many sessions and removes a long, repeated manual sequence
  (a whole report, a multi-tool pipeline, a research-to-draft loop).

### Determinism / gradeability — can success be checked mechanically?
- **1** — fuzzy, no pass/fail, pure judgment every time.
- **3** — mostly repeatable, some judgment; a skill with clear steps.
- **5** — deterministic, fixed trigger, pass/fail, gradeable — a guardrail that
  should fire on an event (a hook).

### Evidence strength — how well is the pattern proven?
- **1** — one session, thin proof.
- **3** — a few sessions, real proof paths.
- **5** — many sessions with concrete proof transcripts and a clearly observed,
  repeated pattern.

### Non-duplication — is this genuinely new?  (a GATE, not just an axis)
- **1** — already covered by an existing skill/hook (a slash command, a matching
  skill in `existing_automations`). **Drop it.** Propose an edit only if the gap is real.
- **3** — partial overlap with something the user has.
- **5** — genuinely new; nothing covers it.

**Gate:** if non-duplication ≤ 2, drop the candidate (put it in `dropped`), do not
score it. Automating what already exists is the worst outcome.

## The score

`score = leverage + determinism + evidence` (range 3–15), after the
non-duplication gate. Emit the per-axis breakdown too, so the re-score pass and a
human can see the reasoning. `confidence` (0–1) is how sure you are the pattern is
real and buildable — below 0.5, drop unless the proof is strong.

## Guardrail proof-bar (read this before proposing any hook)

A guardrail asserts a rule about the user's environment. State only what the proof
shows.

- **Cite the observed pattern.** Read ≤1 proof transcript and name the specific
  thing you saw: "`terraform plan` is run before every `terraform apply` across 12
  sessions." A signature alone (e.g. "Bash error → Bash") cannot tell you the rule.
- **Never assert an environment fact the proof does not contain.** Do not claim a
  command fails, returns a specific exit code, or is unsupported unless the proof
  transcript literally shows that failure. (A real past bug: a guardrail claimed
  `head`/`tail` "exit 127" in the Bash tool — they work fine, and no proof showed
  otherwise. That rule was invented, not observed.) If the failure is not in the
  proof, determinism ≤ 2 — downgrade or drop.

## Three scored reference candidates

Calibrate against these before scoring your shard.

### Weak → DROP
`Glob → Read`, 2 sessions. Pure navigation (the model looking around), carries
`nav_noise`. Leverage 1, determinism 2, evidence 2, non-duplication 3.
**Verdict:** drop — low leverage, no real workflow. (Harness loops — `Task`,
`SendMessage`, `ToolSearch` — are the same call: `harness_noise`, always drop.)

### Mid → KEEP as a skill
`Grep → Read → Edit`, 6 sessions, one project. A real repeatable dev flow that
still needs judgment about what to change. Leverage 4, determinism 3, evidence 4,
non-duplication 4. **score 11, confidence 0.7.** Primitive: **skill** — the steps
are stable but the edit is a judgment call.

### Strong → KEEP as a hook
`terraform plan`, 12 sessions, always run before `terraform apply` (seen in the
proof transcripts). A deterministic check the user runs to gate a risky action.
Leverage 5, determinism 5, evidence 5, non-duplication 5. **score 15, confidence
0.9.** Primitive: **hook** (or **hookify-rule** if `allow_managed_hooks_only`).
Note the proof-bar: the rule is "plan before apply," which the transcripts show —
nothing about exit codes or failure modes is invented.

## Primitive choice

Use [primitive-selection.md](primitive-selection.md): deterministic must-fire
guardrail → hook (hookify-rule under managed policy); repeatable task needing
judgment → skill; recurring multi-step pipeline → skill-chain. Domain does not
matter — a recurring `dbt run` gate is as much a hook as `pytest`, and a weekly
research-to-report loop is as much a skill as a code refactor.
