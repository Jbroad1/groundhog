#!/usr/bin/env bash
# Install groundhog into your Claude Code skills directory:
#   ~/.claude/skills/groundhog
# Run from a clone of the repo:  ./install.sh
set -euo pipefail

src="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dest="$HOME/.claude/skills/groundhog"

mkdir -p "$(dirname "$dest")"
rm -rf "$dest"; mkdir -p "$dest"

# Copy everything except VCS/build cruft.
( cd "$src" && tar --exclude='./.git' --exclude='__pycache__' --exclude='*.pyc' -cf - . ) \
  | ( cd "$dest" && tar -xf - )

echo "Installed groundhog -> $dest"
echo "Verify: python \"$dest/scripts/validate_skill.py\" \"$dest\""
