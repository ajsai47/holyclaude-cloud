"""Critic + Refiner — the decomposition subgraph.

Takes a tasks.json, runs both deterministic (Python) and semantic (LLM)
critiques, and — when issues are found — refines the task list.

Deterministic checks (fast, always run):
  - file_overlap: siblings with no dep relationship both touch the same file
  - dep_cycle: a task transitively depends on itself
  - weak_spec: spec shorter than a sanity threshold
  - orphan_dep: a task references a dep ID that doesn't exist
  - oversized_task: estimated_minutes > 60

Semantic check (LLM call, slower):
  - qualitative critique of task sizing, parallelism potential,
    missing tests, ambiguous specs, scope drift

The module exposes a single `critique_and_refine()` entry point that
combines both, asks the LLM to produce a refined tasks.json if the
critique is non-empty, and returns a structured result the CLI
(and orchestrator skill) can act on.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Flag:
    kind: str
    severity: str   # "warning" | "critical"
    task_ids: list[str]
    message: str

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "task_ids": self.task_ids,
            "message": self.message,
        }


@dataclass
class CritiqueResult:
    flags: list[Flag] = field(default_factory=list)
    refined_tasks: list[dict] | None = None
    llm_summary: str | None = None
    iterations: int = 1

    def to_dict(self) -> dict:
        return {
            "flags": [f.to_dict() for f in self.flags],
            "refined_tasks": self.refined_tasks,
            "llm_summary": self.llm_summary,
            "iterations": self.iterations,
            "converged": len(self.flags) == 0,
        }


# ======================================================================
# Deterministic checks
# ======================================================================

def detect_file_overlaps(tasks: list[dict]) -> list[Flag]:
    """Sibling tasks (no dep relationship either way) that touch the same file."""
    flags: list[Flag] = []
    by_id = {t["id"]: t for t in tasks}

    # Build transitive dependency closure (A depends on B if B in A's transitive deps)
    def deps_closure(tid: str, seen: set[str] | None = None) -> set[str]:
        seen = seen or set()
        task = by_id.get(tid)
        if not task:
            return seen
        for d in task.get("deps", []):
            if d not in seen:
                seen.add(d)
                deps_closure(d, seen)
        return seen

    closures = {t["id"]: deps_closure(t["id"]) for t in tasks}

    # For each pair (A, B) with no dep relationship, check files_touched overlap
    ids = [t["id"] for t in tasks]
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            # Is there any dep relationship (in either direction)?
            if b in closures.get(a, set()) or a in closures.get(b, set()):
                continue
            files_a = set(by_id[a].get("files_touched", []))
            files_b = set(by_id[b].get("files_touched", []))
            overlap = files_a & files_b
            if overlap:
                flags.append(Flag(
                    kind="file_overlap",
                    severity="warning",
                    task_ids=[a, b],
                    message=(
                        f"{a} and {b} both touch {sorted(overlap)} but have "
                        f"no dep relationship — predicted merge conflict. "
                        f"Add a dep edge (usually {b}.deps += [{a}]) to "
                        f"serialize them."
                    ),
                ))
    return flags


def detect_dep_cycles(tasks: list[dict]) -> list[Flag]:
    """Any task that transitively depends on itself."""
    flags: list[Flag] = []
    by_id = {t["id"]: t for t in tasks}

    def has_cycle(start: str, current: str, seen: set[str]) -> bool:
        if current in seen:
            return False
        seen.add(current)
        for d in by_id.get(current, {}).get("deps", []):
            if d == start:
                return True
            if has_cycle(start, d, seen):
                return True
        return False

    for t in tasks:
        if has_cycle(t["id"], t["id"], set()):
            flags.append(Flag(
                kind="dep_cycle",
                severity="critical",
                task_ids=[t["id"]],
                message=f"{t['id']} has a dependency cycle — trace its deps transitively.",
            ))
    return flags


def detect_orphan_deps(tasks: list[dict]) -> list[Flag]:
    """Task references a dep ID that doesn't exist."""
    known = {t["id"] for t in tasks}
    flags: list[Flag] = []
    for t in tasks:
        for d in t.get("deps", []):
            if d not in known:
                flags.append(Flag(
                    kind="orphan_dep",
                    severity="critical",
                    task_ids=[t["id"]],
                    message=f"{t['id']} depends on {d!r} which doesn't exist.",
                ))
    return flags


def detect_weak_specs(tasks: list[dict], min_chars: int = 40) -> list[Flag]:
    """Specs under `min_chars` are probably too vague for a worker to act on."""
    flags: list[Flag] = []
    for t in tasks:
        spec = t.get("spec", "")
        if len(spec.strip()) < min_chars:
            flags.append(Flag(
                kind="weak_spec",
                severity="warning",
                task_ids=[t["id"]],
                message=(
                    f"{t['id']} spec is only {len(spec.strip())} chars — "
                    "probably too vague. Worker will likely over-interpret."
                ),
            ))
    return flags


def detect_oversized(tasks: list[dict], cap_minutes: int = 60) -> list[Flag]:
    """Tasks over `cap_minutes` should probably be split."""
    flags: list[Flag] = []
    for t in tasks:
        mins = t.get("estimated_minutes", 0) or 0
        if mins > cap_minutes:
            flags.append(Flag(
                kind="oversized_task",
                severity="warning",
                task_ids=[t["id"]],
                message=(
                    f"{t['id']} estimated at {mins} min (> {cap_minutes}). "
                    "Consider splitting — long-running workers hit the "
                    "worker_timeout_minutes cap."
                ),
            ))
    return flags


def deterministic_checks(tasks: list[dict]) -> list[Flag]:
    """Run all pure-Python checks."""
    return (
        detect_orphan_deps(tasks)
        + detect_dep_cycles(tasks)
        + detect_file_overlaps(tasks)
        + detect_weak_specs(tasks)
        + detect_oversized(tasks)
    )


# ======================================================================
# LLM critique + refinement
# ======================================================================

CRITIQUE_RESPONSE_RE = re.compile(
    r"\{[\s\S]*?\"flags\"[\s\S]*\}", re.MULTILINE
)


def _frame_critique_prompt(
    tasks: list[dict], goal: str, deterministic: list[Flag]
) -> str:
    tasks_json = json.dumps(tasks, indent=2)
    det_summary = (
        "\n".join(f"- [{f.severity}] [{f.kind}] {f.message}" for f in deterministic)
        if deterministic else "(none detected)"
    )

    return (
        f"You are the LEGION DECOMPOSITION CRITIC. You review a proposed task DAG\n"
        f"produced by a decomposer Claude. Your job is to catch problems BEFORE\n"
        f"workers spawn on a bad graph — every minute of critique saves 5 minutes\n"
        f"of wasted worker time downstream.\n"
        f"\n"
        f"## Original goal\n"
        f"\n"
        f"{goal}\n"
        f"\n"
        f"## Proposed task DAG\n"
        f"\n"
        f"```json\n"
        f"{tasks_json}\n"
        f"```\n"
        f"\n"
        f"## Deterministic checks already run\n"
        f"\n"
        f"{det_summary}\n"
        f"\n"
        f"## What you check (semantic — things static checks can't see)\n"
        f"\n"
        f"1. **Sizing**: are any tasks too big or too small to be useful?\n"
        f"2. **Parallelism**: is the DAG overly serial (chain of deps) when\n"
        f"   siblings could run in parallel?\n"
        f"3. **Ambiguity**: are any specs open to interpretation in ways that\n"
        f"   will produce divergent implementations?\n"
        f"4. **Missing tests**: does any task add code without a matching\n"
        f"   test task (when the repo has test conventions)?\n"
        f"5. **Scope drift**: does any task pull in work beyond the original goal?\n"
        f"6. **Missing integration**: if multiple tasks produce pieces, is there\n"
        f"   a task that integrates / wires them together?\n"
        f"\n"
        f"## Output — JSON ONLY, no prose\n"
        f"\n"
        f"```\n"
        f"{{\n"
        f"  \"flags\": [\n"
        f"    {{\n"
        f"      \"kind\": \"ambiguous_spec\" | \"oversized_task\" | \"under_parallelized\" | \"missing_test\" | \"scope_drift\" | \"missing_integration\" | \"other\",\n"
        f"      \"severity\": \"warning\" | \"critical\",\n"
        f"      \"task_ids\": [\"T-001\", ...],\n"
        f"      \"message\": \"what's wrong + suggested fix\"\n"
        f"    }}\n"
        f"  ],\n"
        f"  \"refined_tasks\": null | [ ... same schema as input ... ],\n"
        f"  \"summary\": \"one sentence overall assessment\"\n"
        f"}}\n"
        f"```\n"
        f"\n"
        f"Rules:\n"
        f"- Only include flags for real issues you'd actually want to fix.\n"
        f"- If you believe the DAG needs changes, include `refined_tasks` as\n"
        f"  the full revised DAG (keep unchanged tasks as-is; modify or add\n"
        f"  tasks as needed; preserve the id-and-deps structure).\n"
        f"- If the DAG looks good: `flags: []`, `refined_tasks: null`.\n"
        f"- Don't flag style nits. Don't propose cosmetic rewrites.\n"
        f"- If deterministic checks already flagged an issue, reinforce it in\n"
        f"  `refined_tasks` (add the missing dep edge, split the oversized task,\n"
        f"  etc.) rather than just re-flagging.\n"
        f"\n"
        f"Output ONLY the JSON, no markdown fences, no prose before or after.\n"
    )


def _parse_critique_response(output: str) -> dict | None:
    """Find the first {...} block containing 'flags'."""
    # Find a position containing "flags" key, then expand outward to balanced braces.
    idx = output.find('"flags"')
    if idx < 0:
        return None
    # Find the { before this position
    start = output.rfind("{", 0, idx)
    if start < 0:
        return None
    # Expand to balanced brace
    depth = 0
    for i, ch in enumerate(output[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(output[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def llm_critique(
    tasks: list[dict], goal: str, deterministic: list[Flag]
) -> dict | None:
    """One LLM call. Returns parsed dict or None on failure."""
    from shutil import which
    if not which("claude"):
        return None
    framed = _frame_critique_prompt(tasks, goal, deterministic)
    try:
        proc = subprocess.run(
            ["claude", "-p", framed,
             "--permission-mode", "bypassPermissions",
             "--output-format", "text"],
            capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    return _parse_critique_response(proc.stdout)


def critique_and_refine(
    tasks: list[dict], goal: str
) -> CritiqueResult:
    """Single iteration: deterministic + LLM critique. Optionally refined tasks."""
    det_flags = deterministic_checks(tasks)
    llm_result = llm_critique(tasks, goal, det_flags)

    result = CritiqueResult()
    result.flags = list(det_flags)

    if llm_result:
        for f in llm_result.get("flags", []) or []:
            result.flags.append(Flag(
                kind=f.get("kind", "other"),
                severity=f.get("severity", "warning"),
                task_ids=f.get("task_ids", []) or [],
                message=f.get("message", ""),
            ))
        if llm_result.get("refined_tasks"):
            result.refined_tasks = llm_result["refined_tasks"]
        result.llm_summary = llm_result.get("summary")

    return result


def iterate_until_stable(
    tasks: list[dict], goal: str, max_iterations: int = 3
) -> CritiqueResult:
    """Run critique + refine up to max_iterations times, or until stable."""
    current = tasks
    for it in range(1, max_iterations + 1):
        result = critique_and_refine(current, goal)
        result.iterations = it

        # If no flags OR no refinement offered: stop.
        if not result.flags:
            return result
        if not result.refined_tasks:
            # Flags exist but critic didn't offer a revision — return as-is.
            return result

        current = result.refined_tasks
        # Continue iterating with the refined set.

    # Hit max iterations — return last result with flags (if any).
    result.refined_tasks = current
    return result
