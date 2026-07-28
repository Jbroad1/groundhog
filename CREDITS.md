# Credits

groundhog is **original work** by Joshua Broad, released under the MIT License.
**All code in `scripts/` was written for this project — no code was copied from
any of the projects below.** What follows credits the *ideas, conventions, and
file formats* that informed the design.

## Licensing honesty

Only two groups of sources have a **locally verifiable** license:

- **hookify** and **hook-development** (Anthropic plugins) — **Apache-2.0**.
- **superpowers: writing-skills** — **MIT, Copyright (c) 2025 Jesse Vincent (obra)**,
  `github.com/obra/superpowers`.

The **ECC** skills below (`origin: ECC` in their frontmatter) ship with **no
LICENSE file** on this machine, so their license could **not** be verified
locally — it is marked **UNKNOWN**. groundhog borrows only their *conventions
and shape*, which is why nothing here depends on their license. **Before copying
any ECC code (as opposed to conventions), confirm ECC's license from its source
repository.** `crune` is external; its license is Apache-2.0 per its repository
(not verified here).

## Attribution table

| Source | Author | License | What we used (conventions only) |
|---|---|---|---|
| **rules-distill** | ECC | UNKNOWN (no local LICENSE) | The "deterministic collect → LLM cross-read → verdict JSON → gated action" phase shape. |
| **continuous-learning-v2** | ECC | UNKNOWN (no local LICENSE) | *Retroactive vs forward* framing; the idea of scrubbing `api_key/token/secret/password/authorization` material from mined observations. Our `scrub.py` is an original implementation. |
| **agent-introspection-debugging** | ECC | UNKNOWN (no local LICENSE) | Its qualitative loop heuristic ("repeated same command", "three times with slight variation") → our `LOOP_RUN = 3`. |
| **automation-audit-ops** | ECC | UNKNOWN (no local LICENSE) | Evidence-first classification; every finding needs a concrete proof path → our `proof_paths`. |
| **hookify / hookify-rules** | Anthropic | Apache-2.0 (upstream) | The `.claude/hookify.{name}.local.md` rule format → emitted rules + `validate_hookify_text`. |
| **hook-development** (`validate-hook-schema.sh`) | Anthropic | Apache-2.0 | The structural checks a hook must pass → reimplemented in pure Python (`validate_hook.py`) because `jq` is unavailable. |
| **superpowers: writing-skills** + Anthropic best practices | Jesse Vincent (obra) | MIT | RED→GREEN→REFACTOR authoring; "description = when to use, not what it does" → [references/authoring-method.md](references/authoring-method.md), enforced by `validate_skill.py`. |
| **crune** (chigichan24/crune) | chigichan24 | Apache-2.0 (per upstream) | Session-log → skill-synthesis as a concept (we output primitives + proofs, not a dashboard). |

## Notes

- groundhog detects these skills if installed (`preflight.py`) and can defer to
  them (e.g. run `skill-comply` for extra QA, defer to a live `writing-skills`).
  None is required — each has a bundled fallback.
- Corrections to attribution or licensing are welcome via issue/PR. If you are an
  ECC maintainer and can confirm a license, please open an issue.
