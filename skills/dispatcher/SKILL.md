---
name: legion-dispatcher
description: Decide whether to run a single legion task as a local Claude Code subagent or as a cloud Modal worker. Used by the orchestrator skill per task. Inputs are the task spec + current swarm load + heuristics from legion.toml. Output is "local" or "cloud" with a one-line reason.
---

# legion-dispatcher

Per-task router. Called once per task by the orchestrator's dispatch loop.

---

## Inputs

- **task**: `{id, title, spec, deps, estimated_minutes, files_touched}`
- **load**: `{in_flight_local, in_flight_cloud, max_workers, throttle_active}`
- **heuristics** from `legion.toml [dispatch]`:
  - `local_file_threshold` (default 5)
  - `cloud_minutes_threshold` (default 5)
  - `always_cloud_patterns` (regex list)

---

## Decision rules

Apply in order. First match wins.

1. **`always_cloud_patterns` matches the title** → `cloud`. Reason: "matched pattern <X>".
2. **Throttle active AND task is read-only / quick** → `local`. Reason: "throttle backoff".
3. **`estimated_minutes` ≤ 2 AND `len(files_touched)` ≤ 2** → `local`. Reason: "trivial task".
4. **Task spec mentions "browser", "scrape", "playwright"** → `cloud`. Reason: "browser work; cloud has dedicated chromium".
5. **Task spec mentions "benchmark", "measure", "profile", "long-running"** → `cloud`. Reason: "throughput-bound".
6. **`len(files_touched)` < `local_file_threshold` AND `estimated_minutes` < `cloud_minutes_threshold`** → `local`. Reason: "small enough for local".
7. **Default** → `cloud`. Reason: "default route".

---

## Output

Return JSON: `{"target": "local" | "cloud", "reason": "..."}`.

If `cloud`:
- The orchestrator will shell out to `modal run ~/holyclaude-cloud/modal/worker.py::run_task` with the task fields.

If `local`:
- The orchestrator will spawn a Claude Code Agent subagent (`general-purpose`) with the framed task prompt and the same constraints (work on a worktree branch, push when done).

---

## Why an LLM call is overkill here

These rules are the prior. For Phase 1, **don't call Opus** — just apply the rules. Adding an LLM tiebreaker is a Phase 2 polish that will marginally improve routing at meaningful cost (one Opus call per task, on top of the per-worker cost). The rules cover ~95% of cases.

If you want to eyeball the routing before dispatch, the orchestrator already shows the task graph with assignments at the human checkpoint.
