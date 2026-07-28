# Authoring method — writing-skills TDD (baked in)

> **Attribution.** This method is adapted from Jesse Vincent's
> **superpowers:writing-skills** and Anthropic's skill-authoring best practices.
> It is reproduced here (in our own words) so groundhog is **standalone** — it
> applies this method whether or not `superpowers` is installed. If
> `writing-skills` *is* present (preflight reports it), defer to the live skill.
> See [CREDITS.md](../CREDITS.md).

**Writing a skill is Test-Driven Development applied to process documentation.**
Every primitive groundhog emits goes through this cycle. So did groundhog
itself.

## The Iron Law

```
NO SKILL WITHOUT A FAILING TEST FIRST
```

Applies to new skills *and* edits. If you wrote the skill before watching an
agent fail without it, you don't know whether it teaches the right thing. Delete
it and start over.

## RED → GREEN → REFACTOR

| Phase | For a skill |
|---|---|
| **RED** | Run the target scenario with a fresh subagent **without** the skill. Record exactly what it does and the rationalizations it uses, verbatim. This is "watch the test fail." |
| **GREEN** | Write the **minimal** skill that addresses those specific failures. Re-run the scenario **with** the skill. The agent should now comply/succeed. |
| **REFACTOR** | Find the next rationalization or gap, add an explicit counter, re-test until bulletproof. Don't add content for hypothetical failures you never observed. |

## Frontmatter rules (enforced by `validate_skill.py`)

- Required: `name` and `description`. Frontmatter ≤ 1024 chars.
- `name`: letters, numbers, hyphens only; verb-first / active where possible
  (`review-pr-diff`, not `pr_review_helper`).
- `description`: **third person, starts with "Use when…", and describes ONLY the
  triggering conditions — never the workflow.** A description that summarizes the
  process becomes a shortcut agents follow *instead of reading the body*.
  - ❌ `Use when reviewing PRs — greps the diff then runs tests then comments`
  - ✅ `Use when reviewing a pull request diff before approving`

## Match the form to the failure

Classify the baseline failure before choosing wording:
- **Skips a rule under pressure** → prohibition + rationalization table + red-flags.
- **Produces the wrong-shaped output** → a positive recipe/contract (state what the
  output *is*), not prohibitions.
- **Omits a required element** → a REQUIRED slot in the template.
- **Behavior should depend on a condition** → a conditional keyed to an
  observable predicate.

## Token efficiency

Descriptions and frequently-loaded skills load into every prompt window. Keep the
SKILL.md body lean (< 500 words where possible); push heavy reference and code to
separate files (like this one). One excellent example beats five mediocre ones.

## Testing with subagents (the RED/GREEN gate)

- Discipline skills: pressure scenarios (time + sunk-cost + authority) — does the
  agent comply under stress?
- Technique skills: application scenarios — can a fresh agent apply it correctly?
- Always include a **no-skill control**. If the control doesn't fail, there's
  nothing to teach — don't write the skill.
- Read every flagged result manually; 5+ reps (single samples lie).

## Checklist

- [ ] RED: baseline scenario run without the skill; failures documented verbatim.
- [ ] GREEN: minimal skill written to those failures; scenario now passes.
- [ ] REFACTOR: new rationalizations countered; re-tested.
- [ ] Frontmatter valid (`validate_skill.py` clean).
- [ ] Body lean; heavy detail in references; one strong example.
