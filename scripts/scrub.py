#!/usr/bin/env python3
"""
scrub.py -- secret scrubbing for groundhog.

Every piece of free text extracted from your session data (prompts, Bash
commands, tool_result output, error messages) is passed through ``scrub_text``
/ ``scrub_obj`` before it is written to scan.json. This is best-effort: it
removes common secret *shapes* from the artifact that the analyzer and
(potentially) a published repo will see, but a bare keyless/opaque token can
still survive -- review scan.json before sharing it.

Design: the credential-shaped patterns here are ORIGINAL. The *approach* --
regex-scrub api_key / token / secret / password / authorization material out of
mined observations -- follows a convention also used by the ``continuous-
learning-v2`` skill's observation scrubber. No code was copied (that skill ships
no local license). See CREDITS.md.

Pure stdlib. Runnable standalone::

    python scripts/scrub.py path/to/file        # scrub a file to stdout
    echo "token=abc123def456" | python scripts/scrub.py --stats
"""
from __future__ import annotations

import argparse
import re
import sys

REDACT = "[REDACTED]"

# Order matters: multiline key blocks first, then shaped tokens, then the
# credential-in-URL rule and the generic key=value fallback (the broadest).
#
# NOTE: this is best-effort redaction of *known secret shapes*. A bare,
# keyless, high-entropy token (a raw hex/base64 string with no keyword and no
# recognizable prefix) is NOT caught -- see scrub.py's callers and the README's
# "review before sharing" note. Do not describe this as complete redaction.
_PATTERNS: list[tuple[str, re.Pattern, str]] = [
    (
        "private-key-block",
        # Cap is generous (100k) rather than tight: the old 4000 cap failed
        # *open* on long keys (RSA-8192, annotated/encrypted PEM, OpenSSH keys
        # with comments, concatenated blobs), passing the whole key through.
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"
            r"[\s\S]{0,100000}?-----END (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"
        ),
        "[REDACTED-PRIVATE-KEY]",
    ),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}"), "[REDACTED-OPENAI-KEY]"),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), "[REDACTED-ANTHROPIC-KEY]"),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), "[REDACTED-GITHUB-TOKEN]"),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "[REDACTED-GITHUB-PAT]"),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED-AWS-KEY]"),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), "[REDACTED-GOOGLE-KEY]"),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "[REDACTED-SLACK-TOKEN]"),
    ("stripe-key", re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"), "[REDACTED-STRIPE-KEY]"),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}"),
        "[REDACTED-JWT]",
    ),
    (
        "authorization-header",
        re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+|basic\s+|token\s+)?[A-Za-z0-9._+/=-]{8,}"),
        r"\1[REDACTED]",
    ),
    # Credentials embedded in a URL: scheme://user:PASSWORD@host. Redacts only
    # the password so the rest of the URL stays legible. Catches git-clone URLs
    # and DB connection strings (postgres://, mysql://, mongodb://, ...).
    #
    #   * The greedy password class [^\s/]+ backtracks to the LAST '@' in the
    #     authority, so a password containing '@' (postgres://user:p@ssw0rd@host)
    #     is redacted whole instead of leaking its tail.
    #   * (@[^\s@/]*) keeps the host token but allows an EMPTY host, so a
    #     host-omitted DSN (postgresql://user:secret@/db?host=/sock -- a real
    #     Unix-socket connection string) is still redacted rather than leaking
    #     the password before the '@'.
    #   * The scheme repeat is bounded ({0,31}) so a long run of scheme-like
    #     characters that contains no '://' cannot cause O(n^2) rescanning
    #     (ReDoS) on unbounded mined prompt text. Every real URI scheme is short.
    #   * A stray '@' in a path/query (.../api?email=a@b.com) stays untouched
    #     because [^\s/]+ cannot cross the '/'.
    (
        # `[^\s:/@]*:` (was `+:`) also catches the empty-username DSN form
        # `redis://:password@host` / `mongodb://:pw@host`, the sibling of the
        # empty-host case handled by `(@[^\s@/]*)`.
        "url-credentials",
        re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]{0,31}://[^\s:/@]*:)([^\s/]+)(@[^\s@/]*)"),
        r"\1[REDACTED]\3",
    ),
    # Generic "<secret-ish key> = <value>" fallback. Keeps the key + operator,
    # redacts the ENTIRE value (quoted string or bareword up to whitespace/
    # separator) so punctuation-heavy passwords can't partially survive. No
    # leading \b, so env-var keys like DB_PASSWORD / MYSQL_PWD are caught; the
    # optional ["'] after the key catches JSON-shaped secrets ("password": "x").
    (
        "key-value-secret",
        re.compile(
            r"(?i)((?:api[_-]?key|apikey|secret[_-]?key|access[_-]?key|secret|"
            r"access[_-]?token|refresh[_-]?token|auth[_-]?token|"
            r"client[_-]?secret|private[_-]?key|password|passwd|pwd|token)"
            r"""["']?\s*[:=]\s*)"""
            r"""(?:"[^"]+"|'[^']+'|[^\s"';,]+)"""
        ),
        r"\1[REDACTED]",
    ),
]


def scrub_text(text: str) -> tuple[str, int]:
    """Return (scrubbed_text, number_of_redactions)."""
    if not text or not isinstance(text, str):
        return text, 0
    hits = 0
    for _name, pat, repl in _PATTERNS:
        text, n = pat.subn(repl, text)
        hits += n
    return text, hits


def scrub_obj(obj):
    """Recursively scrub every string inside a JSON-like structure.

    Returns a new structure; input is not mutated. Dict keys are left intact
    (they carry structure, not secrets), values and list items are scrubbed.
    """
    if isinstance(obj, str):
        return scrub_text(obj)[0]
    if isinstance(obj, dict):
        return {k: scrub_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_obj(v) for v in obj]
    return obj


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Scrub secrets from text.")
    ap.add_argument("file", nargs="?", help="File to scrub (default: stdin).")
    ap.add_argument("--stats", action="store_true", help="Print redaction count to stderr.")
    args = ap.parse_args(argv)

    if args.file:
        with open(args.file, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    else:
        raw = sys.stdin.read()

    out, hits = scrub_text(raw)
    sys.stdout.write(out)
    if args.stats:
        print(f"[scrub] {hits} redaction(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
