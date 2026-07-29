#!/usr/bin/env python3
"""
mine_workflows.py -- the deterministic heart of groundhog.

Combines the three scans (history / transcripts / plans) into a single scored,
secret-scrubbed ``scan.json`` of candidate workflows. Every candidate carries:

  * a stable ``signature`` and human ``title`` / ``summary``,
  * ``frequency`` (how many sessions / occurrences),
  * ``steps_saved`` / ``automatability`` / ``already_automated`` factors,
  * ``leverage`` = frequency x steps_saved (the raw prize: how much manual work
    automating this removes),
  * a ``score`` = frequency x steps_saved x automatability x (1 - already_automated),
  * a *provisional* ``recommended_primitive`` (skill / hook / hookify-rule /
    skill-chain) that the analyzer subagent later refines, and
  * one or more ``proof_paths`` (file + line + session) so any claim is checkable.

Candidate kinds mined:
  tool-sequence, repeated-call-loop, edit-hotspot, bash-template, error-fix,
  prompt-cluster, plan-type.

The scorer is deliberately domain-agnostic. It attenuates harness/agent-plumbing
loops and pure-navigation loops (not real workflows), treats guardrails as a
STRUCTURAL signal rather than a dev-CI allowlist, and ranks leverage-forward so
research/report work is not sunk below developer work. See the constants below.

The output is deterministic: identical input data -> byte-identical candidate
ordering (leverage-forward rank, stable signature tie-break). This is what the
unit tests pin.

The loop-detection threshold reuses the repeated-call heuristic documented in the
``agent-introspection-debugging`` skill. Secret scrubbing follows the approach of
``continuous-learning-v2``. See CREDITS.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import groundhog_lib as fl
from scan_history import scan_history
from scan_transcripts import scan_transcripts
from scan_plans import scan_plans
from scrub import scrub_obj

# --- tuning knobs (deterministic) ------------------------------------------ #
MIN_SESSIONS_SEQ = 3        # an n-gram must recur in this many sessions
MAX_NGRAM = 5
LOOP_RUN = 3                # >= this many identical consecutive calls == a loop
MIN_EDITS_HOTSPOT = 4
MIN_BASH_RECUR = 4
MIN_ERRFIX = 3
MIN_PROMPT_CLUSTER = 3
MIN_PLAN_CLUSTER = 2
MAX_PROOFS = 5

# Automatability weight per kind (0..1): how mechanically a candidate converts
# into an automation. Deterministic must-fire guardrails sit highest; everything
# that captures a repeatable task sits in a deliberately COMPRESSED band so no
# domain is structurally sunk below another. (Earlier weights ranked developer
# kinds well above research/prompt kinds — a shape bias. Automatability no longer
# drives the ranking either; ranking is leverage-forward. See mine().)
_AUTOMATABILITY = {
    "bash-guardrail": 0.85,      # deterministic check that must fire -> hook
    "repeated-call-loop": 0.80,  # a manual loop -> encapsulate
    "error-fix": 0.75,           # a recurring error->fix is a guardrail candidate
    "plan-type": 0.75,           # a recurring pipeline -> skill-chain
    "tool-sequence": 0.70,
    "prompt-cluster": 0.70,      # a recurring request (incl. research/report) -> skill
    "edit-hotspot": 0.65,
    "bash-template": 0.60,
}

# --- guardrails: a STRUCTURAL signal, not a dev-CI allowlist ---------------- #
# A recurring deterministic command that shows up across many DISTINCT sessions
# reads as a habitual check the user runs to gate their work — that wants to fire
# on an event (a hook), whatever the domain (`terraform plan`, `dbt run`, a data-
# validation script, as much as `pytest`). Dev-CI verbs are a confidence BOOSTER
# that raises certainty, never a gate: a non-dev command that recurs the same way
# reaches the hook tier too.
_GUARD_MIN_SESSIONS = 3
_GUARD_TOKENS = ("test", "lint", "format", "prettier", "eslint", "ruff", "mypy",
                 "tsc", "typecheck", "vitest", "jest", "pytest", "validate",
                 "build", "gofmt", "clippy", "black", "flake8")
# Match tokens as WHOLE WORDS: "test" must not fire on "latest", "build" not on
# "rebuild". (`pytest` still matches its own token; "npm test" matches "test".)
_GUARD_RE = re.compile(r"\b(?:" + "|".join(_GUARD_TOKENS) + r")\b")

# --- noise: loops/sequences that are not real workflows --------------------- #
# Harness / agent-orchestration tools. A sequence or loop built ENTIRELY from
# these is the agent driving its own plumbing (subagent dispatch, task
# bookkeeping, skill lookup), not a user workflow. On multi-agent histories this
# is the #1 source of high-frequency noise, so we attenuate it hard AND flag it
# (`harness_noise`) so the analyzer drops it fast. We also stopped promoting
# Skill/Task/Agent sequences to skill-chains for the same reason (see _recommend).
_HARNESS_TOOLS = {"ToolSearch", "SendMessage", "Task", "Agent",
                  "TaskCreate", "TaskUpdate", "TaskOutput", "Skill"}
_HARNESS_ATTEN = 0.05

# Pure-navigation read-only tools. A loop/sequence of ONLY these is the model
# looking around, not a workflow. NOTE: WebSearch / WebFetch are deliberately
# ABSENT — a research/report loop (search -> fetch -> read -> synthesise) is a
# legitimate workflow to automate, not navigation noise.
_NAV_TOOLS = {"Glob", "Grep", "Read", "LS", "NotebookRead"}
_NAV_ATTEN = 0.3


def _noise_class(tools) -> str:
    """Classify a tool bag: 'harness' (agent plumbing), 'nav' (pure navigation),
    or '' (a real workflow). An empty bag is not noise."""
    ts = [t for t in tools if t]
    if not ts:
        return ""
    if all(t in _HARNESS_TOOLS for t in ts):
        return "harness"
    if all(t in _NAV_TOOLS for t in ts):
        return "nav"
    return ""


def _noise_factor(cls: str) -> float:
    return {"harness": _HARNESS_ATTEN, "nav": _NAV_ATTEN}.get(cls, 1.0)


# --------------------------------------------------------------------------- #
def _guard_boost(tmpl: str) -> bool:
    """Dev-CI verb present as a WHOLE WORD? A booster for guardrail confidence, not
    a gate. Substring matching wrongly fired on 'latest'/'rebuild'/'attestation'."""
    return bool(_GUARD_RE.search(tmpl.lower()))


def _guardish(tmpl: str, sessions: int) -> bool:
    """Structural guardrail signal: a deterministic command habitual enough to
    recur across sessions, OR one carrying a dev-CI verb. Either reaches the hook
    tier; being a dev command is not required."""
    return sessions >= _GUARD_MIN_SESSIONS or _guard_boost(tmpl)


def _bash_steps_saved(tmpl: str) -> int:
    """Data-driven leverage for a recurring command: how many manual pieces
    wrapping it removes. Count pipeline segments + flags, so a multi-stage piped
    command saves more than a bare one-word command. (Was hard-coded 2.)"""
    segs = sum(1 for s in re.split(r"\|\||&&|[;|]", tmpl) if s.strip())
    flags = len(re.findall(r"(?:^|\s)-{1,2}[A-Za-z][\w-]*", tmpl))
    return max(1, min(segs + flags, 8))


def _prompt_steps_saved(sample: str) -> int:
    """Data-driven leverage for a recurring request: estimate the manual steps it
    implies from its structure (commas, and/then/after conjunctions, numbered
    items). A multi-clause ask ('research X, summarise, then draft') saves more
    than a one-liner. (Was hard-coded 2.)"""
    parts = re.split(r",|\band\b|\bthen\b|\bafter\b|;|\d+[.)]", (sample or "").lower())
    clauses = sum(1 for p in parts if len(p.strip()) > 3)
    return max(2, min(clauses, 10))


def _overlap_ratio(tokens: set, existing: set) -> float:
    tokens = {t for t in tokens if t and len(t) > 2}
    if not tokens:
        return 0.0
    hit = len(tokens & existing)
    return round(min(1.0, hit / len(tokens)), 3)


def _recommend(kind: str, signature, evidence: dict, env: dict | None):
    """Provisional primitive + rationale. The analyzer subagent refines this."""
    managed = bool(env and env.get("allow_managed_hooks_only"))

    def as_hook(reason):
        if managed:
            return "hookify-rule", (reason + " NOTE: allowManagedHooksOnly is set on "
                    "this machine, so a settings-level hook may be inert; emit a "
                    "runtime-read hookify rule instead."), True
        return "hook", reason, False

    if kind == "bash-template":
        if evidence.get("guardish"):
            return as_hook("A deterministic check that recurs across sessions reads as a "
                           "must-fire guardrail.")
        return "skill", ("A recurring command worth wrapping in a skill so its flags and "
                         "context are captured."), False
    if kind == "error-fix":
        return as_hook("A recurring error->resolution pair suggests a guardrail that "
                       "catches the mistake before it happens.")
    if kind == "plan-type":
        return "skill-chain", ("A recurring multi-step plan shape is best captured as a "
                               "skill-chain that sequences sub-skills."), False
    if kind == "tool-sequence":
        # A long, repeatable multi-step sequence reads as a skill-chain. We no
        # longer promote a sequence just because it contains Skill/Task/Agent —
        # that is harness plumbing (see _HARNESS_TOOLS), and promoting it floated
        # orchestration noise to the top masquerading as buildable work.
        if len(list(signature)) >= 4:
            return "skill-chain", ("A long, repeatable multi-step sequence fits a "
                                   "skill-chain that sequences its steps."), False
        return "skill", ("A repeatable multi-step sequence that needs judgment fits a "
                         "skill."), False
    if kind == "repeated-call-loop":
        return "skill", ("A repeated manual loop is worth encapsulating in a skill so it "
                         "runs once, correctly."), False
    # edit-hotspot, bash-template, prompt-cluster
    return "skill", ("A recurring task that benefits from captured, judgment-driven "
                     "steps fits a skill."), False


def _score(kind, frequency, steps_saved, already_automated, factor=1.0):
    auto = _AUTOMATABILITY.get(kind, 0.5) * factor
    return round(frequency * steps_saved * auto * (1.0 - already_automated), 2), round(auto, 3)


def _finish(cand, kind, frequency, steps_saved, already, env, factor=1.0):
    prim, rationale, managed_warn = _recommend(kind, cand.get("signature"), cand.get("evidence", {}), env)
    score, auto = _score(_AUTOMATABILITY_KEY(kind, cand), frequency, steps_saved, already, factor)
    leverage = frequency * steps_saved
    cand.update({
        "kind": kind,
        "frequency": frequency,
        "steps_saved": steps_saved,
        "leverage": leverage,
        "automatability": auto,
        "already_automated": already,
        "score": score,
        "recommended_primitive": prim,
        "primitive_rationale": rationale,
        "managed_hook_warning": managed_warn,
    })
    # Leverage-forward rank key (stripped before output). The real prize is
    # frequency x steps_saved; we deliberately do NOT rank by `score`, which folds
    # in `automatability` — a per-kind shape weight that pushed research/prompt
    # work below dev work ("score fights leverage"). We DO keep the noise
    # attenuation (harness/nav loops) and the already-automated suppressor, so
    # plumbing and already-solved work don't consume top-N shard slots.
    cand["_rank"] = leverage * factor * (1.0 - already)
    return cand


def _AUTOMATABILITY_KEY(kind, cand):
    # bash templates split into guardrail vs general for weighting
    if kind == "bash-template" and cand.get("evidence", {}).get("guardish"):
        return "bash-guardrail"
    return kind


# --------------------------------------------------------------------------- #
def _sess_meta(sessions):
    meta = []
    for s in sessions:
        meta.append({
            "proj": fl.project_slug(s.get("cwd") or s.get("project_dir", "")),
            "session_id": s.get("session_id", ""),
            "file": s.get("file", ""),
        })
    return meta


def _is_contig_sub(short: tuple, long: tuple) -> bool:
    n, m = len(short), len(long)
    if n >= m:
        return False
    return any(long[i:i + n] == short for i in range(m - n + 1))


def _mine_sequences(sessions, meta, existing, env, min_sessions):
    ngram = defaultdict(lambda: {"sessions": set(), "count": 0, "proofs": []})
    for si, s in enumerate(sessions):
        seq, lines, f, sid = s["sequence"], s["lines"], s["file"], s["session_id"]
        L = len(seq)
        for n in range(2, MAX_NGRAM + 1):
            for i in range(L - n + 1):
                w = tuple(seq[i:i + n])
                if len(set(w)) == 1:        # all-identical -> loop candidate handles it
                    continue
                e = ngram[w]
                e["sessions"].add(si)
                e["count"] += 1
                if len(e["proofs"]) < MAX_PROOFS:
                    e["proofs"].append({"file": f,
                                        "line": lines[i] if i < len(lines) else None,
                                        "session": sid, "detail": " -> ".join(w)})
    raw = []
    for w, e in ngram.items():
        if len(e["sessions"]) < min_sessions:
            continue
        raw.append((w, e))
    # closed-sequence suppression: drop a shorter n-gram fully contained in a
    # longer kept one with comparable session support.
    raw.sort(key=lambda t: (-len(t[0]), -len(t[1]["sessions"])))
    kept = []
    for w, e in raw:
        dominated = any(
            _is_contig_sub(w, kw) and len(e["sessions"]) <= len(ke["sessions"]) * 1.25
            for kw, ke in kept)
        if not dominated:
            kept.append((w, e))
    cands = []
    for w, e in kept:
        freq = len(e["sessions"])
        projs = sorted({meta[si]["proj"] for si in e["sessions"]})[:8]
        already = _overlap_ratio(set(x.lower() for x in w), existing)
        cls = _noise_class(w)
        cand = {
            "signature": list(w),
            "title": " -> ".join(w),
            "summary": f"Tool sequence '{' -> '.join(w)}' repeated across {freq} sessions "
                       f"({e['count']} occurrences).",
            "projects": projs,
            "proof_paths": e["proofs"][:MAX_PROOFS],
            "evidence": {"occurrences": e["count"], "sessions": freq,
                         "harness_noise": cls == "harness", "nav_noise": cls == "nav"},
        }
        cands.append(_finish(cand, "tool-sequence", freq, len(w), already, env,
                             _noise_factor(cls)))
    return cands


def _mine_loops(sessions, meta, existing, env):
    tool_loops = defaultdict(lambda: {"sessions": set(), "max_run": 0, "proofs": []})
    for si, s in enumerate(sessions):
        seq, lines, f, sid = s["sequence"], s["lines"], s["file"], s["session_id"]
        i = 0
        L = len(seq)
        while i < L:
            j = i
            while j + 1 < L and seq[j + 1] == seq[i]:
                j += 1
            run = j - i + 1
            if run >= LOOP_RUN:
                e = tool_loops[seq[i]]
                e["sessions"].add(si)
                e["max_run"] = max(e["max_run"], run)
                if len(e["proofs"]) < MAX_PROOFS:
                    e["proofs"].append({"file": f, "line": lines[i] if i < len(lines) else None,
                                        "session": sid, "detail": f"{seq[i]} x{run} in a row"})
            i = j + 1
    cands = []
    for tool, e in tool_loops.items():
        freq = len(e["sessions"])
        projs = sorted({meta[si]["proj"] for si in e["sessions"]})[:8]
        already = _overlap_ratio({tool.lower()}, existing)
        steps = min(e["max_run"], 8)
        cls = _noise_class([tool])
        cand = {
            "signature": [tool, "loop"],
            "title": f"Repeated {tool} loop (up to {e['max_run']} in a row)",
            "summary": f"{tool} was called {LOOP_RUN}+ times consecutively in {freq} "
                       f"session(s) (max run {e['max_run']}) -- a manual loop worth "
                       f"encapsulating.",
            "projects": projs,
            "proof_paths": e["proofs"][:MAX_PROOFS],
            "evidence": {"max_run": e["max_run"], "sessions": freq,
                         "harness_noise": cls == "harness", "nav_noise": cls == "nav"},
        }
        cands.append(_finish(cand, "repeated-call-loop", freq, steps, already, env,
                             _noise_factor(cls)))
    return cands


def _mine_edits(sessions, meta, existing, env):
    hot = defaultdict(lambda: {"sessions": set(), "count": 0, "projects": set(), "proofs": []})
    for si, s in enumerate(sessions):
        f, sid = s["file"], s["session_id"]
        for ed in s["edits"]:
            p = ed["path"]
            if not p:
                continue
            e = hot[p]
            e["sessions"].add(si)
            e["count"] += 1
            e["projects"].add(meta[si]["proj"])
            if len(e["proofs"]) < MAX_PROOFS:
                e["proofs"].append({"file": f, "line": ed.get("line"), "session": sid,
                                    "detail": f"edit {p}"})
    cands = []
    for path, e in hot.items():
        if e["count"] < MIN_EDITS_HOTSPOT:
            continue
        freq = len(e["sessions"])
        already = _overlap_ratio(set(fl.slugify(path).split("-")), existing)
        cand = {
            "signature": ["edit", path],
            "title": f"Edit hotspot: {path}",
            "summary": f"{path} was edited {e['count']} times across {freq} session(s) "
                       f"in {len(e['projects'])} project(s).",
            "projects": sorted(e["projects"])[:8],
            "proof_paths": e["proofs"][:MAX_PROOFS],
            "evidence": {"edit_count": e["count"], "sessions": freq},
        }
        cands.append(_finish(cand, "edit-hotspot", freq, min(e["count"], 8), already, env))
    return cands


def _mine_bash(sessions, meta, existing, env):
    tmpls = defaultdict(lambda: {"sessions": set(), "count": 0, "proofs": []})
    for si, s in enumerate(sessions):
        f, sid = s["file"], s["session_id"]
        for b in s["bash"]:
            t = b["tmpl"]
            if not t:
                continue
            e = tmpls[t]
            e["sessions"].add(si)
            e["count"] += 1
            if len(e["proofs"]) < MAX_PROOFS:
                e["proofs"].append({"file": f, "line": b.get("line"), "session": sid,
                                    "detail": t})
    cands = []
    for tmpl, e in tmpls.items():
        if e["count"] < MIN_BASH_RECUR:
            continue
        freq = len(e["sessions"])
        guard = _guardish(tmpl, freq)
        boosted = _guard_boost(tmpl)
        already = _overlap_ratio(set(fl.slugify(tmpl).split("-")), existing)
        cand = {
            "signature": ["bash", tmpl],
            "title": f"Recurring command: {tmpl[:70]}",
            "summary": f"'{tmpl[:80]}' ran {e['count']} times across {freq} session(s)"
                       + (" and recurs like a guardrail check." if guard else "."),
            "projects": sorted({meta[si]["proj"] for si in e["sessions"]})[:8],
            "proof_paths": e["proofs"][:MAX_PROOFS],
            "evidence": {"run_count": e["count"], "sessions": freq,
                         "guardish": guard, "guard_boosted": boosted},
        }
        cands.append(_finish(cand, "bash-template", freq, _bash_steps_saved(tmpl), already, env))
    return cands


def _mine_errfix(sessions, meta, existing, env):
    pairs = defaultdict(lambda: {"sessions": set(), "count": 0, "proofs": []})
    for si, s in enumerate(sessions):
        f, sid = s["file"], s["session_id"]
        for ef in s["error_fixes"]:
            key = (ef["err"], ef["fix"])
            e = pairs[key]
            e["sessions"].add(si)
            e["count"] += 1
            if len(e["proofs"]) < MAX_PROOFS:
                e["proofs"].append({"file": f, "line": ef.get("line"), "session": sid,
                                    "detail": f"{ef['err']} error -> {ef['fix']}"})
    cands = []
    for (err, fix), e in pairs.items():
        if e["count"] < MIN_ERRFIX:
            continue
        freq = len(e["sessions"])
        already = _overlap_ratio({err.lower(), fix.lower()}, existing)
        cand = {
            "signature": ["errfix", err, fix],
            "title": f"Error->fix: {err} fails, then {fix}",
            "summary": f"A {err} error was followed by {fix} {e['count']} times across "
                       f"{freq} session(s) -- a recurring mistake worth guarding.",
            "projects": sorted({meta[si]["proj"] for si in e["sessions"]})[:8],
            "proof_paths": e["proofs"][:MAX_PROOFS],
            "evidence": {"pair_count": e["count"], "sessions": freq},
        }
        cands.append(_finish(cand, "error-fix", freq, 3, already, env))
    return cands


def _mine_prompts(hist, existing, env):
    clusters = defaultdict(lambda: {"count": 0, "projects": set(), "sample": "",
                                    "is_slash": False, "slash": ""})
    for p in hist.get("prompts", []):
        c = clusters[p["norm"]]
        c["count"] += 1
        c["projects"].add(p["project_slug"])
        if not c["sample"]:
            c["sample"] = p["sample"]
            c["is_slash"] = p["is_slash"]
            c["slash"] = p["slash"]
    cands = []
    hist_path = hist.get("path", "history.jsonl")
    for norm, c in clusters.items():
        if c["count"] < MIN_PROMPT_CLUSTER:
            continue
        # slash-command clusters are already an automation -> mark, don't propose to rebuild
        already = 1.0 if c["is_slash"] else _overlap_ratio(set(norm.split()), existing)
        cand = {
            "signature": ["prompt", norm[:60]],
            "title": (f"Recurring slash command: {c['slash']}" if c["is_slash"]
                      else f"Recurring request: {c['sample'][:60]}"),
            "summary": f"{c['count']} similar prompts across {len(c['projects'])} project(s): "
                       f"\"{c['sample'][:80]}\"",
            "projects": sorted(c["projects"])[:8],
            "proof_paths": [{"file": hist_path, "line": None, "session": "",
                             "detail": c["sample"][:100]}],
            "evidence": {"prompt_count": c["count"], "is_slash": c["is_slash"]},
        }
        cands.append(_finish(cand, "prompt-cluster", c["count"],
                             _prompt_steps_saved(c["sample"]), already, env))
    return cands


def _mine_plans(plans, existing, env):
    clusters = defaultdict(lambda: {"count": 0, "files": [], "headings": set(), "steps": []})
    for p in plans.get("plans", []):
        c = clusters[p["norm"]]
        c["count"] += 1
        if len(c["files"]) < MAX_PROOFS:
            c["files"].append(p["file"])
        c["headings"].update(p["headings"][:6])
        c["steps"].append(p["n_steps"])
    cands = []
    for norm, c in clusters.items():
        if c["count"] < MIN_PLAN_CLUSTER:
            continue
        avg_steps = round(sum(c["steps"]) / max(1, len(c["steps"])))
        already = _overlap_ratio(set(norm.split()), existing)
        cand = {
            "signature": ["plan", norm[:50]],
            "title": f"Recurring plan type: {norm[:60]}",
            "summary": f"{c['count']} approved plans share this shape (~{avg_steps} steps): "
                       f"{', '.join(sorted(c['headings'])[:4])}.",
            "projects": [],
            "proof_paths": [{"file": f, "line": None, "session": "", "detail": norm[:80]}
                            for f in c["files"]],
            "evidence": {"plan_count": c["count"], "avg_steps": avg_steps},
        }
        cands.append(_finish(cand, "plan-type", c["count"], min(max(avg_steps, 2), 12),
                             already, env))
    return cands


# --------------------------------------------------------------------------- #
# Verdict memory (index-layer). A candidate's evidence_fingerprint captures what
# we knew when we last decided; if it matches the ledger, a re-run can surface the
# prior decision instead of re-analysing from scratch.
def _evidence_fingerprint(cand: dict) -> str:
    payload = json.dumps({
        "kind": cand.get("kind"),
        "signature": cand.get("signature"),
        "frequency": cand.get("frequency"),
        "steps_saved": cand.get("steps_saved"),
        "evidence": cand.get("evidence", {}),
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _attach_verdict_memory(store, candidates) -> None:
    from index_store import sig_key
    ledger = store.all_verdicts()
    for c in candidates:
        fp = _evidence_fingerprint(c)
        c["evidence_fingerprint"] = fp
        v = ledger.get(sig_key(c["signature"]))
        if v:
            c["prior_verdict"] = {
                "primitive": v.get("primitive"),
                "decision": v.get("decision"),
                "reason": v.get("reason"),
                "confidence": v.get("confidence"),
                "evidence_unchanged": (v.get("evidence_fingerprint") == fp),
                "decided_at": v.get("decided_at"),
            }


def _materialize_aggregates(store, candidates) -> None:
    rows = []
    for c in candidates:
        ev = c.get("evidence", {})
        occ = (ev.get("occurrences") or ev.get("run_count") or ev.get("edit_count")
               or ev.get("pair_count") or ev.get("prompt_count") or ev.get("plan_count")
               or c.get("frequency", 0))
        rows.append((c.get("kind", ""), "|".join(map(str, c.get("signature", []))),
                     c.get("frequency", 0), occ, c.get("steps_saved", 0),
                     c.get("proof_paths", [])[:MAX_PROOFS]))
    store.replace_aggregates(rows)


def mine(config_dir: Path, since_ms: int | None = None, cache: dict | None = None,
         env: dict | None = None, min_sessions: int = MIN_SESSIONS_SEQ,
         store=None) -> dict:
    hist = scan_history(config_dir, since_ms)
    if store is not None:
        trans = scan_transcripts(config_dir, since_ms, store=store)
    else:
        trans = scan_transcripts(config_dir, since_ms, cache=cache)
    plans = scan_plans(config_dir, since_ms)

    sessions = trans["sessions"]
    meta = _sess_meta(sessions)
    project_paths = sorted({s.get("cwd", "") for s in sessions if s.get("cwd")})
    inventory = fl.inventory_automations(config_dir, project_paths)
    existing = fl.existing_signatures(inventory)

    candidates = []
    candidates += _mine_sequences(sessions, meta, existing, env, min_sessions)
    candidates += _mine_loops(sessions, meta, existing, env)
    candidates += _mine_edits(sessions, meta, existing, env)
    candidates += _mine_bash(sessions, meta, existing, env)
    candidates += _mine_errfix(sessions, meta, existing, env)
    candidates += _mine_prompts(hist, existing, env)
    candidates += _mine_plans(plans, existing, env)

    # Leverage-forward, deterministic ordering. Primary key is the leverage-rank
    # (frequency x steps_saved, noise-attenuated, already-automated-suppressed) so
    # the top-N slice `shard` hands to the analyzers is the real prize, not the
    # automatability-folded score. `score` breaks ties; the signature text makes
    # it stable (identical input -> identical order).
    candidates.sort(key=lambda c: (-c["_rank"], -c["score"], "|".join(map(str, c["signature"]))))
    for idx, c in enumerate(candidates, start=1):
        c["id"] = f"cand-{idx:04d}"
        del c["_rank"]  # internal ranking scratch; never emitted
    # move id to front for readability
    candidates = [{**{"id": c.pop("id")}, **c} for c in candidates]

    # Index-layer memory: remember prior verdicts and roll up the aggregates so a
    # re-run surfaces old decisions instead of re-asking. Legacy path leaves the
    # candidate shape untouched.
    if store is not None:
        _attach_verdict_memory(store, candidates)
        _materialize_aggregates(store, candidates)

    scan = {
        "schema_version": fl.SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "since_ms": since_ms,
        "config_dir": str(config_dir),
        "env": env or {},
        "sources": {
            "history": {"exists": hist["exists"], "count": hist["count"], "total": hist["total"]},
            "transcripts": {"exists": trans["exists"], "files_total": trans["files_total"],
                            "files_in_window": trans["files_in_window"],
                            "files_parsed": trans["files_parsed"],
                            "cache_hits": trans["cache_hits"],
                            "sessions": len(sessions)},
            "plans": {"exists": plans["exists"], "count": plans["count"], "total": plans["total"]},
        },
        "existing_automations": {
            "skills": len(inventory["skills"]),
            "hooks": len(inventory["hooks"]),
            "hookify_rules": len(inventory["hookify_rules"]),
            "skill_names": sorted(s["name"] for s in inventory["skills"])[:500],
        },
        "candidate_count": len(candidates),
        "candidates": candidates,
    }
    # Belt-and-suspenders: scrub the whole scan once more. scrub_obj walks the
    # entire structure, so token-shaped secrets in the config_dir/env metadata
    # are redacted too -- not just the candidate payloads. Non-string values are
    # left untouched, and candidate strings (already scrubbed at extraction) carry
    # no live secret for this second pass to expose.
    scan = scrub_obj(scan)
    return scan


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Mine repeated workflows -> scan.json.")
    ap.add_argument("--config-dir")
    ap.add_argument("--since", default=fl.RECENCY_DEFAULT)
    ap.add_argument("--out", help="Write scan.json here (default: print summary).")
    ap.add_argument("--min-sessions", type=int, default=MIN_SESSIONS_SEQ)
    args = ap.parse_args(argv)
    cfg = fl.resolve_config_dir(args.config_dir)
    scan = mine(cfg, fl.parse_since(args.since), min_sessions=args.min_sessions)
    if args.out:
        fl.write_json(Path(args.out), scan)
        print(f"wrote {args.out} ({scan['candidate_count']} candidates)")
    else:
        print(f"{scan['candidate_count']} candidates (top 10):")
        for c in scan["candidates"][:10]:
            print(f"  [{c['score']:>7.2f}] {c['recommended_primitive']:<12} "
                  f"{c['kind']:<18} {c['title'][:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
