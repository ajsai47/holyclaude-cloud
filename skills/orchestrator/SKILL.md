---
name: legion-orchestrator
description: Decompose a coding goal into a parallelizable task graph and dispatch workers. Use when the user wants to spin up a swarm via /legion-start, run multiple Claude workers in parallel on a single project, or coordinate a fleet of cloud Claudes. The orchestrator runs locally; workers can be local subagents or Modal cloud containers (decided per-task by the dispatcher skill).
---

# legion-orchestrator

The brain of HolyClaude-Cloud. Lives locally. Decomposes goals into tasks. Hands tasks to the dispatcher.

---

## Inputs

One of:
- A goal string ("implement OAuth flow with Google + GitHub")
- A path to a `/plan` output (preferred — already structured)
- A path to a markdown file with a task list

Plus implicit inputs from the working directory:
- `legion.toml` — config (max_workers, human_checkpoint, etc.)
- `.git/` — repo to swarm against
- `.legion/` — working state for this run (created if missing)

---

## Procedure

### 1. Load config + repo

Read `legion.toml` from the cwd. If missing, copy from `~/holyclaude-cloud/config/legion.toml.example` and tell the user.

Resolve the GitHub repo URL: `legion.toml` `[reconciler] github_repo`, falling back to `git remote get-url origin`.

Resolve the base branch: `git rev-parse --abbrev-ref HEAD` (or `main` if detached).

### 2. Decompose into tasks

If input is already a structured task list (numbered markdown, JSON), parse it directly. Otherwise call Opus with this frame:

> Decompose the following goal into atomic, parallelizable tasks. Each task should:
> - Touch a bounded set of files (ideally <5).
> - Be completable in <30 minutes by a focused agent.
> - Have a one-line title and a 1-3 paragraph spec.
> - List dependencies on other tasks by ID.
>
> Output JSON: `[{"id": "T-001", "title": "...", "spec": "...", "deps": [], "estimated_minutes": N, "files_touched": [...]}]`

Save to `.legion/tasks.json`.

### 3. Show the task graph + checkpoint

Print the task graph to the user — IDs, titles, dependency arrows, estimated minutes, total wall-clock estimate at current `max_workers`.

If `human_checkpoint_after_decompose = true` (default):
- Use AskUserQuestion: "Dispatch this graph? [yes / edit / cancel]"
- On `edit`: open `.legion/tasks.json` in the user's editor; re-read after they save.
- On `cancel`: stop. Leave `.legion/tasks.json` for next time.

If `human_checkpoint_after_decompose = false`: dispatch immediately, log "yolo mode engaged".

### 4. Dispatch loop

Maintain a queue of ready tasks (deps satisfied). Until queue empty AND no in-flight workers:

```
while ready_queue or in_flight:
  while len(in_flight) < current_max_workers and ready_queue:
    task = ready_queue.pop()
    decision = call dispatcher skill (task, repo state, current_load)
    if decision == "local":
      spawn Claude Code Agent subagent on the task (general-purpose agent)
    else:  # "cloud"
      shell out to: /Users/ajsai47/tinker-env/bin/modal run \\
        ~/holyclaude-cloud/modal/worker.py::run_task \\
        --task-id ... --title ... --prompt ... --repo-url ... --base-branch ...
      track the modal run id
    in_flight.add(task)

  wait for any worker to finish (poll PR state for cloud, agent return for local)
  on finish:
    update .legion/state.json
    if status == "shipped": mark deps satisfied; promote ready tasks
    if status == "claude_failed" or "no_changes": surface to user, mark task failed
    if status == "blocked": read .legion/blockers/<task-id>.md, surface to user

  if ramp_first_run and current_max_workers < max_workers and no_throttle_observed:
    current_max_workers += 1   # gentle ramp
```

Use the **claude-peers MCP** (already in HolyClaude) to make in-flight workers visible to other Claude Code sessions on the machine.

### 5. Handoff to reconciler

When all tasks complete, the dispatch loop's job ends. The reconciler skill (separate) takes over: orders PRs by dep graph, attempts merges, spawns mediator on conflicts.

For Phase 1, just print the list of open PR URLs and let the user merge manually. Reconciler is Phase 2.

---

## State files

```
.legion/
├── tasks.json              # the decomposition
├── state.json              # current run: in-flight, completed, failed
├── transcripts/            # per-task transcripts pulled from worker cache
│   └── T-001.jsonl
├── blockers/               # tasks the worker couldn't do
│   └── T-003.md
└── prs.json                # task-id -> PR URL mapping
```

`state.json` is the single source of truth for `/legion-status` and `/legion-stop`.

---

## Failure modes to handle

- **Modal not authed:** `~/holyclaude-cloud/setup` failed or wasn't run. Tell user to run it.
- **legion.toml missing:** offer to copy the example.
- **Dirty working tree:** orchestrator refuses to start unless `git status` is clean. Workers branch off the current HEAD; uncommitted local changes won't be in their world.
- **No `claude-pro-session` secret:** setup didn't push it. Tell user to re-run setup.
- **Throttle (worker logs show 429):** halve `current_max_workers` for 10 min. Document as a Phase 2 watcher; for Phase 1, just surface the 429 to the user and pause.
