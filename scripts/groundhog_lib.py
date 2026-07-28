"""
groundhog_lib -- shared helpers for groundhog.

Pure Python 3 standard library only. No third-party packages, no `jq`, no shell
callouts. Every path is treated as a native OS path (works with Windows
``C:\\Users\\...`` and POSIX ``/home/...`` alike); we never assume MSYS-style
``/c/...`` rewriting.

The module intentionally has zero dependency on ECC / superpowers so the whole
skill runs on a vanilla Claude Code install.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "1"
RECENCY_DEFAULT = "90d"
STATE_DIRNAME = "groundhog"  # under the resolved config dir


# --------------------------------------------------------------------------- #
# Config-dir resolution (portable)
# --------------------------------------------------------------------------- #
def resolve_config_dir(explicit: str | None = None) -> Path:
    """Resolve the Claude config dir.

    Precedence: explicit arg -> $CLAUDE_CONFIG_DIR -> ~/.claude.
    Returns an absolute, native Path. Does not require the dir to exist so
    callers can produce a clean error themselves.
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / ".claude").resolve()


def state_dir(config_dir: Path) -> Path:
    """Where groundhog keeps its cache / manifest / last-run artifacts."""
    return config_dir / STATE_DIRNAME


def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


# --------------------------------------------------------------------------- #
# JSONL streaming (memory-safe: transcripts can be tens of MB)
# --------------------------------------------------------------------------- #
def iter_jsonl(path: Path):
    """Yield (line_number, parsed_obj) for each valid JSON line.

    Streams the file; never loads it whole. Malformed lines are skipped
    silently (real transcripts occasionally contain partial writes).
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield i, json.loads(line)
                except (ValueError, TypeError):
                    continue
    except (OSError, UnicodeError):
        return


def read_json(path: Path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path: Path, obj) -> None:
    ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)  # atomic on same volume


# --------------------------------------------------------------------------- #
# Time handling. history.jsonl uses epoch-ms ints; transcripts use ISO-8601.
# --------------------------------------------------------------------------- #
_DUR_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_UNIT_MS = {"s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _iso_ready(s: str) -> str:
    """Normalize an ISO-8601 string for ``datetime.fromisoformat``.

    Pre-3.11 fromisoformat is strict: it rejects a trailing ``Z`` and accepts
    only 3- or 6-digit fractional seconds. Swap ``Z``->``+00:00`` and pad/trim
    any fractional part to exactly 6 digits so history/transcript timestamps
    parse on Python 3.8-3.10 as well as 3.11+.
    """
    s = s.replace("Z", "+00:00")
    m = re.search(r"\.(\d+)", s)
    if m:
        frac = (m.group(1) + "000000")[:6]
        s = s[:m.start()] + "." + frac + s[m.end():]
    return s


def parse_since(spec: str | None) -> int | None:
    """Return a cutoff in epoch-ms, or None for 'no cutoff'.

    Accepts durations like ``30d`` / ``12h`` / ``2w`` (relative to now),
    an ISO date ``2026-07-01``, or the literal ``all`` / ``None``.
    """
    if spec is None:
        return None
    spec = str(spec).strip()
    if spec.lower() in ("all", "none", ""):
        return None
    m = _DUR_RE.match(spec)
    if m:
        return now_ms() - int(m.group(1)) * _UNIT_MS[m.group(2).lower()]
    # try ISO date / datetime (normalized for pre-3.11 fromisoformat)
    iso = _iso_ready(spec)
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def to_epoch_ms(value) -> int | None:
    """Normalize a record timestamp (int ms, numeric str, or ISO str) to ms."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    iso = _iso_ready(s)
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Normalization: turn volatile tool inputs into stable signatures for mining.
# --------------------------------------------------------------------------- #
_FILE_TOOLS = {"Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "NotebookRead"}
_PATH_KEYS = ("file_path", "path", "notebook_path", "filePath")

_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_ABS_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|/)[^\s'\"]+")
_QUOTED_RE = re.compile(r"""(['"]).*?\1""", re.DOTALL)
_HASH_RE = re.compile(r"\b[0-9a-fA-F]{7,40}\b")
_NUM_RE = re.compile(r"\b\d+\b")
_URL_RE = re.compile(r"https?://[^\s'\"]+")


def repo_relative(path_str: str, cwd: str | None) -> str:
    """Map an absolute path to something comparable across runs/machines.

    If the path lives under ``cwd`` we return the POSIX-style relative path;
    otherwise we return the basename. Keeps hotspot clustering stable without
    leaking full user directory layouts.
    """
    if not path_str:
        return ""
    p = path_str.replace("\\", "/")
    if cwd:
        c = cwd.replace("\\", "/").rstrip("/")
        if p.lower().startswith(c.lower() + "/"):
            return p[len(c) + 1:]
    # not under cwd -> basename keeps signal without the volatile prefix
    return p.rsplit("/", 1)[-1] if "/" in p else p


def template_bash(cmd: str) -> str:
    """Collapse a concrete shell command into a template so near-identical
    invocations cluster together.

    ``git commit -m "fix: thing"`` and ``git commit -m "other"`` both become
    ``git commit -m <STR>``. We keep the executable + subcommands + flags,
    which carry the intent, and mask the volatile operands.
    """
    if not cmd:
        return ""
    s = cmd.strip()
    s = _URL_RE.sub("<URL>", s)
    s = _QUOTED_RE.sub("<STR>", s)
    s = _ABS_PATH_RE.sub("<PATH>", s)
    s = _HASH_RE.sub("<HASH>", s)
    s = _NUM_RE.sub("<N>", s)
    s = re.sub(r"\s+", " ", s).strip()
    # keep it bounded
    return s[:200]


def file_path_from_input(name: str, tool_input: dict) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    if name in _FILE_TOOLS:
        for k in _PATH_KEYS:
            if tool_input.get(k):
                return str(tool_input[k])
    return None


# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #
def slugify(text: str, maxlen: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:maxlen].rstrip("-")) or "candidate"


def project_slug(path: str) -> str:
    """A short, human label for a project path (its last 1-2 segments)."""
    if not path:
        return "unknown"
    parts = re.split(r"[\\/]+", path.rstrip("\\/"))
    parts = [p for p in parts if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else "unknown")


# --------------------------------------------------------------------------- #
# Frontmatter + existing-automation inventory (used for dedup and validation)
# --------------------------------------------------------------------------- #
def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse a leading ``---`` YAML-ish frontmatter block.

    Deliberately minimal (no PyYAML dependency): handles ``key: value`` scalars
    with optional quotes, and inline lists ``[a, b]``. Returns (mapping, body).
    If there is no frontmatter, returns ({}, original_text).
    """
    text = text.lstrip("﻿")  # tolerate a UTF-8 BOM (common on Windows editors)
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict = {}
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
            val = val[1:-1]
        elif val.startswith("[") and val.endswith("]"):
            val = [v.strip().strip("\"'") for v in val[1:-1].split(",") if v.strip()]
        meta[key] = val
    if end is None:
        return {}, text
    body = "\n".join(lines[end + 1:])
    return meta, body


def _iter_skill_files(config_dir: Path, cap: int = 4000):
    seen = 0
    skills_dir = config_dir / "skills"
    if skills_dir.is_dir():
        for p in sorted(skills_dir.glob("*/SKILL.md")):
            yield p
            seen += 1
            if seen >= cap:
                return
    plugins_dir = config_dir / "plugins"
    if plugins_dir.is_dir():
        for p in plugins_dir.rglob("SKILL.md"):
            yield p
            seen += 1
            if seen >= cap:
                return


def inventory_automations(config_dir: Path, project_paths=None) -> dict:
    """Best-effort inventory of automations that already exist, so we never
    recommend re-building something the user already has.

    Returns {"skills": [...], "hooks": [...], "hookify_rules": [...]}.
    """
    skills = []
    for sp in _iter_skill_files(config_dir):
        try:
            meta, _ = parse_frontmatter(sp.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        name = meta.get("name") or sp.parent.name
        skills.append({
            "name": str(name),
            "description": str(meta.get("description", ""))[:400],
            "path": str(sp),
        })

    hooks = []
    for fname in ("settings.json", "settings.local.json", "remote-settings.json"):
        data = read_json(config_dir / fname)
        if not isinstance(data, dict):
            continue
        hook_cfg = data.get("hooks")
        if not isinstance(hook_cfg, dict):
            continue
        for event, entries in hook_cfg.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                hooks.append({
                    "event": event,
                    "matcher": entry.get("matcher", ""),
                    "source": fname,
                })

    hookify_rules = []
    for proj in (project_paths or [])[:200]:
        try:
            cdir = Path(proj) / ".claude"
            if cdir.is_dir():
                for rp in cdir.glob("hookify.*.local.md"):
                    hookify_rules.append({"name": rp.stem, "path": str(rp), "project": proj})
        except (OSError, ValueError):
            continue

    return {"skills": skills, "hooks": hooks, "hookify_rules": hookify_rules}


def existing_signatures(inventory: dict) -> set:
    """Flatten an inventory into a bag of lowercase tokens for cheap
    'already automated?' overlap scoring in the deterministic miner."""
    toks = set()
    for s in inventory.get("skills", []):
        toks.update(re.split(r"[^a-z0-9]+", (s.get("name", "") + " " + s.get("description", "")).lower()))
    for h in inventory.get("hooks", []):
        toks.update(re.split(r"[^a-z0-9]+", (str(h.get("matcher", "")) + " " + str(h.get("event", ""))).lower()))
    toks.discard("")
    return toks


if __name__ == "__main__":  # tiny self-check
    assert template_bash('git commit -m "fix: x"') == "git commit -m <STR>"
    assert repo_relative("C:/proj/src/a.py", "C:/proj") == "src/a.py"
    assert parse_since("all") is None
    assert parse_since("1d") is not None
    assert to_epoch_ms("2026-07-01T00:00:00Z") is not None
    assert to_epoch_ms(1759138194478) == 1759138194478
    print("groundhog_lib self-check OK")
