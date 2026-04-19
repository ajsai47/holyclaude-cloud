---
name: legion-orchestrator
description: Decompose a coding goal into a parallelizable task DAG, then hand off to the autonomous `legion run` loop. Use when the user wants to spin up a swarm via /legion-start, run multiple Claude workers in parallel, or coordinate a fleet of cloud Claudes. The heavy lifting — spawn, poll, reconcile, merge, mediate, re-dispatch — lives in the `legion run` CLI subcommand.
---

# legion-orchestrator

You (Claude) handle the three things a CLI can't:
1. **Decompose a goal** into a task DAG (needs judgment).
2. **Show the user the graph** and checkpoint before dispatch.
3. **Narrate the run** as it progresses and surface failures at the end.

Everything else is `legion run` in a tight loop, driven by Python.

---

## Step 0 — Preflight

```bash
git status --porcelain              # must be empty
test -f ./legion.toml || cp ~/holyclaude-cloud/config/legion.toml.example ./legion.toml
/Users/ajsai47/tinker-env/bin/modal secret list | grep -q claude-pro-session
```

Any failure → stop and tell the user how to fix (usually: commit/stash, copy legion.toml, or run `~/holyclaude-cloud/setup`).

---

## Step 1 — Resume check

```bash
test -f .legion/state.json
```

If it exists:
- `--resume` flag: skip to Step 5 (run loop).
- No flag: AskUserQuestion: "Existing run detected. [resume / wipe and start fresh / cancel]"

If it doesn't: fresh decomposition flow.

---

## Step 2 — Decompose

Read the user's input (goal string, plan file, or tasks.json). If already structured, skip to Step 3.

Otherwise, decompose. You're Opus — do this yourself, don't shell out. Write `.legion/tasks.json`:

```json
[
  {
    "id": "T-001",
    "title": "one-line title",
    "spec": "1-3 paragraphs of context the worker needs",
    "deps": [],
    "estimated_minutes": 15,
    "files_touched": ["path/to/file.ts"]
  }
]
```

Constraints:
- Each task < 30 min, touches < 10 files.
- Tasks form a DAG; `deps` reference earlier task IDs.
- Siblings with no shared deps will run in parallel.
- Don't over-decompose — if the whole goal is 3 files of work, it's one task.
- Task specs should be self-contained — the worker sees the spec alone, not the surrounding goal.

---

## Step 3 — Init

```bash
mkdir -p .legion
# write the tasks.json you produced above
~/holyclaude-cloud/bin/legion init .legion/tasks.json
```

---

## Step 4 — Show graph + checkpoint

Render the task DAG for the user. ASCII edges + estimated wall-clock at current cap (`legion cap`). Example:

```
T-001 ─┬─► T-003
       └─► T-004 ─► T-006
T-002 ────────────► T-005 ─► T-006

6 tasks, max depth 3, est. 35 min wall-clock at cap=3
```

If `[swarm] human_checkpoint_after_decompose = true` AND no `--yolo`:
- `AskUserQuestion("Dispatch this graph? [yes / edit / cancel]")`
- `edit`: open `.legion/tasks.json` in the user's editor; after save, `rm -rf .legion && legion init` again.
- `cancel`: stop.
- `yes`: proceed.

Otherwise: log "yolo mode engaged" and proceed.

---

## Step 5 — Run

Hand off to the autonomous loop:

```bash
~/holyclaude-cloud/bin/legion run --tick-seconds 10
```

This is foreground — stdout streams live. It prints:
- `[run] spawned T-XXX → local|cloud` when a task dispatches
- `[tick N] in_flight=X ready=Y shipped=Z merged=W` every tick with change
- `LEGION RUN COMPLETE` + summary at end

Let it run. **Don't try to drive the loop yourself** — it handles everything the CLI handles: spawn, poll, reconcile, mediate, re-dispatch on CI fail, ramp, throttle backoff, graceful stop.

Exits when:
- All tasks terminal (merged, failed, or blocker), OR
- `.legion/STOP` written (by `/legion-stop` or Ctrl-C), OR
- `--max-ticks` reached.

---

## Step 6 — Report

Read the final summary. Surface to the user:
- Total merged PRs (with URLs).
- Any task with `merge_blocker = "ci_failed"` that exceeded retries → user needs to fix the underlying test.
- Any task with `merge_blocker = "mediator_maxed"` → conflict needs human resolution, point at `.legion/mediator_logs/<task-id>.log`.
- Total elapsed time + parallel-worker-minutes consumed.

---

## What to do NOT do

- Don't call `legion spawn`, `legion poll`, `legion reconcile` manually — `legion run` handles all three.
- Don't hand-write the dispatch loop in bash — we tried that, it's ~80 lines of awk and sleep and miscounts.
- Don't decompose in a subprocess / shell out — you're Opus, just think.
- Don't skip the preflight when `--resume` — state might be from a different repo or before a `legion.toml` change. Still check.

---

## Subcommand reference

```
legion init <tasks.json> [--repo-url URL] [--base-branch BR]
legion run [--tick-seconds N] [--max-ticks M] [--quiet]
legion ready                 # JSON list of ready task IDs
legion route <task-id>       # {"target": ..., "reason": ...}
legion spawn <task-id> [--target local|cloud]
legion poll                  # updates in-flight, emits changes
legion cap                   # dynamic cap + slots
legion status                # human-readable table
legion scale <n|auto>        # override max_workers, 'auto' to clear
legion cost                  # usage summary
legion reconcile             # one idempotent merge pass
legion mediate <task-id>     # force a mediator run
legion stop [--graceful|--force]
legion cleanup [--all]
```

`legion run` = the 6 main verbs (spawn + poll + reconcile + mediate + ramp + stop) interleaved in a loop. You invoke it once.
