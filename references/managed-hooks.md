# Managed-hooks policy (`allowManagedHooksOnly`)

Some machines ship a managed policy that sets `allowManagedHooksOnly: true`
(seen here in `~/.claude/remote-settings.json`). When active, **user- and
plugin-level hooks configured in `settings.json` may be silently inert** — only
managed hooks run. A guardrail you "install" as a settings hook would then never
fire, and nothing tells you.

groundhog treats this as a first-class constraint.

## How groundhog handles it

1. **Detect** — `preflight.py` reads `settings.json`, `managed-settings.json`,
   and `remote-settings.json`; if any sets `allowManagedHooksOnly: true`, it
   records `allow_managed_hooks_only = true` and the source file.
2. **Warn** — every hook candidate carries `managed_hook_warning: true`, and the
   scan/install output prints the warning with the source file.
3. **Prefer a runtime-read rule** — when the policy is active, a hook candidate's
   `recommended_primitive` becomes `hookify-rule` instead of `hook`. A hookify
   rule is a markdown file (`.claude/hookify.<name>.local.md`) read at runtime by
   the hookify mechanism, so it is **not** subject to the settings-hook
   restriction.
4. **Refuse to write an inert hook** — `groundhog.py install` **skips** any `hook`
   artifact while the policy is active and tells you to use a hookify rule.

## Decision flow

```
allowManagedHooksOnly active?
├─ no  → emit a normal settings hook (validate_hook.py), merge into settings.json
└─ yes → is a hookify mechanism available? (preflight optional_skills["hookify-rules"])
         ├─ yes → emit a hookify rule (.claude/hookify.<name>.local.md)
         └─ no  → do NOT install an inert hook. Either:
                  • author the guardrail as a SKILL the user invokes, or
                  • advise installing a runtime-read hook mechanism first.
```

## Why not just write the hook anyway?

Because it would look installed but do nothing — the worst failure mode for a
guardrail. groundhog would rather emit a primitive that actually fires (a
hookify rule or a skill) and be explicit about the trade-off than hand you a
security/quality guardrail that is silently dead.
