# Contributing to groundhog

Thanks for your interest. groundhog is a zero-dependency, pure-Python-standard-library
Claude Code skill, and contributions that keep it that way are very welcome.

## Ground rules

- **Zero dependencies.** Standard library only — no `pip install`, no `jq`, no
  external frameworks. If you find yourself reaching for a third-party package,
  open an issue to discuss first; there is almost always a stdlib path.
- **Tests first.** Add or update tests for any behavior change. The suite is plain
  `unittest`, so it runs on a vanilla Python 3.8+ with nothing installed.
- **Cross-platform.** Code runs on Linux, macOS, and Windows with native paths and
  no shelling out. CI covers Python 3.8–3.14 on all three.

## Dev setup

No setup beyond Python 3.8+:

```bash
git clone https://github.com/Jbroad1/groundhog
cd groundhog
python -m unittest discover -s tests -v     # full test suite
python evals/run_evals.py                    # deterministic eval gate (a1–a8)
python -m compileall scripts tests evals     # byte-compile smoke check
```

## Before you open a PR

- [ ] `python -m unittest discover -s tests -v` passes
- [ ] `python evals/run_evals.py` passes (8/8)
- [ ] No new third-party dependencies
- [ ] Tests added or updated for the change
- [ ] Docs updated if behavior changed
- [ ] No real secrets in fixtures or tests — the sample data uses obviously
      synthetic, fake tokens on purpose

## Reporting a security issue

Please do not open a public issue for a real vulnerability. Use GitHub's private
reporting instead: the repository's **Security → Report a vulnerability** tab. See
also the honest limits of the secret scrubber in the README ("Is my data safe?").
