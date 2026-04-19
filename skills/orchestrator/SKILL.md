---
name: legion-orchestrator
description: Decompose a coding goal into a parallelizable task graph and run a concurrent dispatch loop. Use when the user wants to spin up a swarm via /legion-start, run multiple Claude workers in parallel on a single project, or coordinate a fleet of cloud Claudes. The orchestrator runs locally; workers can be local subprocesses or Modal cloud containers (decided per-task by routing rules).
---

# legion-orchestrator

The brain of HolyClaude-Cloud. You (Claude) are the orchestrator. The heavy lifting — state persistence, routing rules, spawn, poll, governor — lives in the `legion` CLI at `~/holyclaude-cloud/bin/legion`. Your job is the loop: decompose, dispatch, poll, reconcile.

---

## Inputs

One of:
- A goal string from the user ("implement OAuth with Google + GitHub").
- A path to a structured plan (a `/plan` output, a markdown task list, or a `tasks.json`).

Plus:
- `./legion.toml` — config (or defaults)
- `.git/` — the repo we're swarming
- `~/holyclaude-cloud/bin/legion` — the CLI

---

## Procedure

### Step 0 — Preflight

```bash
git status --porcelain       # must be empty; if dirty, stop and tell user to commit/stash
ls ./legion.toml             # if missing, copy from ~/holyclaude-cloud/config/legion.toml.example
/Users/ajsai47/tinker-env/bin/modal secret list | grep claude-pro-session
# ...if missing: tell user to run ~/holyclaude-cloud/setup
```

Also check `ls .legion/state.json` — if it exists from a prior run, ask the user whether to resume (not yet implemented — Phase 4) or wipe (`rm -rf .legion`) and start fresh.

### Step 1 — Decompose

If the input is already `tasks.json` or parseable task list, skip to step 2.

Otherwise, decompose. Use your own judgment — you're Opus. Output a JSON list to `.legion/tasks.json`:

```json
[
  {
    "id": "T-001",
    "title": "Add OAuth callback handler",
    "spec": "Wire /api/auth/callback to exchange Google's code for tokens...",
    "deps": [],
    "estimated_minutes": 15,
    "files_touched": ["api/auth.ts", "config.ts"]
  },
  ...
]
```

Constraints when decomposing:
- Each task < 30 minutes, touches < 10 files.
- Tasks form a DAG — `deps` references earlier task IDs.
- Siblings with no shared deps can parallelize.
- Don't over-decompose — if the whole goal is 3 files of work, it's one task.

### Step 2 — Initialize run state

```bash
mkdir -p .legion
# ...write tasks.json to .legion/tasks.json
~/holyclaude-cloud/bin/legion init .legion/tasks.json
```

### Step 3 — Show the graph + human checkpoint

Render the task graph to the user (ASCII DAG or numbered list with dep arrows). Show estimated wall-clock at the current cap.

Read `legion.toml`:
- `[swarm] human_checkpoint_after_decompose = true` (default): AskUserQuestion "Dispatch this graph? [yes / edit / cancel]".
  - `edit`: open `.legion/tasks.json` in the user's editor; re-read after they save.
  - `cancel`: stop cleanly.
  - `yes`: proceed.
- `false` (yolo mode): log "yolo mode engaged — dispatching without checkpoint" and proceed.

If `--yolo` was passed to `/legion-start`, override config and proceed.

### Step 4 — Dispatch loop

This is the main loop. Drive it entirely through the CLI:

```
loop:
  # Check for stop
  if [ -f .legion/STOP ]; then break

  # How many slots do we have?
  cap_json=$(~/holyclaude-cloud/bin/legion cap)
  slots=$(echo "$cap_json" | jq -r .slots_available)

  # Get ready tasks
  ready_ids=$(~/holyclaude-cloud/bin/legion ready | jq -r '.[]')

  # Spawn up to `slots` of them
  for id in $ready_ids; do
    [ "$slots" -le 0 ] && break
    ~/holyclaude-cloud/bin/legion spawn $id
    slots=$((slots - 1))
  done

  # If nothing is in flight AND no ready AND no pending-with-unmet-deps left, we're done
  status=$(~/holyclaude-cloud/bin/legion cap)
  in_flight=$(echo "$status" | jq -r .in_flight)
  if [ "$in_flight" -eq 0 ] && [ -z "$ready_ids" ]; then
    # Check if any pending tasks remain (deps never satisfied — cycle or failed deps)
    # If so, surface to user. Otherwise, done.
    break
  fi

  # Poll in-flight
  sleep 15
  changes=$(~/holyclaude-cloud/bin/legion poll)
  # Surface any status changes to the user (new PRs, failures, throttle)

  # Show status every few iterations so the user sees progress
```

Narrate progress to the user as you go:
- "Spawned T-001 (cloud) — fc-abc123"
- "T-001 shipped → https://github.com/.../pull/142"
- "T-003 failed — see .legion/blockers/T-003.md"
- "Throttle detected — halving cap to 2"

Use the `claude-peers` MCP (already in HolyClaude) to broadcast legion state so other Claude Code sessions on the machine see what the swarm is doing.

### Step 5 — Final report

When the loop exits, print:

```
LEGION COMPLETE
  Shipped:     N PRs opened
  No changes:  M tasks
  Failed:      K tasks (see .legion/blockers/)
  Elapsed:     Xm Ys wall-clock
  Parallel:    Zm worker-minutes

PRs:
  - T-001: https://github.com/.../pull/142
  - T-002: https://github.com/.../pull/143
  ...
```

Phase 1/2 stops here. The user merges PRs manually, in dependency order. Phase 3 adds the Reconciler (ordered auto-merge + mediator for conflicts).

---

## Failure handling

### Worker throttle mid-loop

The governor detects 429s in worker logs on each `legion poll` and engages a 10-min backoff (halves the cap). Your dispatch loop will automatically slow down because `legion cap` returns the lower number. Surface the throttle to the user but don't panic — it's expected above ~3 concurrent workers on Pro.

### Cloud worker hangs past worker_timeout_minutes

`legion poll` flags stale in-flight tasks. On seeing `stale_in_flight` in the changes list, you can force-kill one: `legion stop --force` kills ALL — there's no per-task force in Phase 2. If one task is stuck, manually cancel its Modal FunctionCall: `/Users/ajsai47/tinker-env/bin/modal call cancel <fc-id>`.

### Deps that never satisfy (failed prerequisite)

If T-001 fails and T-002/T-003 depend on T-001, they stay in `pending` forever. Surface this to the user: "3 tasks blocked by T-001 failure — dropping them." Mark them `cancelled` via a helper you can write inline (update `.legion/state.json` under the lock — or just tell the user to re-run with `T-001` fixed).

### Dirty tree after local worker

Local workers run in `.legion/worktrees/<task-id>/`. The user's main worktree is untouched. If a local worker leaves its worktree in a weird state, delete it: `git worktree remove --force .legion/worktrees/T-NNN`.

---

## Subcommand reference

```
legion init <tasks.json> [--repo-url URL] [--base-branch BR]
legion ready                 # prints JSON list of task IDs with deps satisfied
legion route <task-id>       # prints {"target": "local"|"cloud", "reason": "..."}
legion spawn <task-id> [--target local|cloud]   # target auto-routed if omitted
legion poll                  # updates in-flight, emits change list
legion cap                   # current dynamic cap + slots available
legion status                # human-readable status table
legion scale <n>             # override max_workers
legion cost                  # cost summary
legion stop [--graceful|--force]
```

Nothing in this skill writes to `.legion/state.json` directly. Everything goes through the CLI, which holds a file lock during mutations. Safe against concurrent CLI invocations (e.g. the user running `/legion-status` while the dispatch loop is polling).
