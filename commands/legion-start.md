---
name: legion-start
description: Spin up a HolyClaude legion. Decompose a goal into tasks, dispatch local + cloud workers, reconcile, ship. Use --resume to continue an existing run.
argument-hint: "<goal OR path to task file> [--yolo] [--resume] [--auth api|session]"
---

# /legion-start

Start (or resume) the swarm.

---

## Load skill

Load the `legion-orchestrator` skill. Also be aware of `legion-dispatcher` and `legion-reconciler`. Read gotchas before running.

---

## Parse arguments

$ARGUMENTS

Flags:
- `--resume` — skip decomposition; continue from existing `.legion/state.json`.
- `--yolo` — override `human_checkpoint_after_decompose = true` for this run.
- `--auth api|session` — override `[swarm] auth_mode` for this run (does NOT persist to `legion.toml`).

Positional: goal string OR path to `tasks.json` / `/plan` output / markdown task list.

If neither `--resume` nor a goal/file is provided, STOP and ask what to build.

---

## Procedure

### 1. Preflight

```bash
git status --porcelain               # must be empty — refuse if dirty
test -f ./legion.toml                # copy from ~/holyclaude-cloud/config/legion.toml.example if missing
modal secret list | grep claude-pro-session  # else tell user to run ~/holyclaude-cloud/setup
```

### 2. Resume vs fresh

If `.legion/state.json` exists:
- If `--resume` passed: skip to step 5 (legion run)
- Otherwise ask: "An existing run is present in .legion/. Resume it, or wipe and start fresh?"

Otherwise proceed with fresh decomposition.

### 3. Decompose

Read the goal (or file). You're Opus — decompose into atomic parallelizable tasks. Write `.legion/tasks.json`:

```json
[{"id": "T-001", "title": "...", "spec": "...", "deps": [], "estimated_minutes": N, "files_touched": [...]}, ...]
```

Rules:
- Each task < 30 min, touches < 10 files.
- Form a DAG; `deps` references earlier task IDs.
- Don't over-decompose — if the whole goal is 3 files, it's one task.

### 4. Init + checkpoint

```bash
~/holyclaude-cloud/bin/legion init .legion/tasks.json
```

Show the user an ASCII DAG of tasks + estimated wall-clock. Then:

- If `human_checkpoint_after_decompose = true` AND `--yolo` NOT passed: `AskUserQuestion("Dispatch this graph? [yes / edit / cancel]")`.
  - `edit`: open `.legion/tasks.json` in editor, re-read after save, then `rm -rf .legion && legion init` again.
  - `cancel`: stop cleanly.
  - `yes`: proceed.
- Otherwise: log "yolo mode engaged" and proceed.

### 5. Run

Launch the autonomous loop:

```bash
~/holyclaude-cloud/bin/legion run --tick-seconds 10
```

This runs in the foreground and drives the full dispatch + poll + reconcile + merge loop:
- Each tick: spawn up to `cap` ready tasks, poll in-flight, reconcile shipped PRs (merge in dep order, mediate conflicts, re-dispatch CI failures).
- Exits when: no in-flight AND no ready AND no shipped-unmerged remain, OR `.legion/STOP` is present, OR Ctrl-C.
- Auto-ramps from 1 worker up to `max_workers` as successes accumulate.
- Auto-shrinks on throttle detection.

Narrate progress from the tick output to the user. At the end, it prints a `LEGION RUN COMPLETE` summary with each merged PR URL and any failures with reasons. Show that to the user.

### 6. Final report

If any tasks ended with `merge_blocker = "ci_failed"` or `"mediator_maxed"`, surface them prominently with pointers:
- CI failed max retries: the PR needs human fixes. Show the PR URL + latest check link.
- Mediator maxed: the conflict couldn't be auto-resolved. Show the PR URL + `.legion/mediator_logs/<task-id>.log`.

---

## Failure handling

- **Modal not authed**: run `~/holyclaude-cloud/setup` first.
- **Dirty tree**: workers branch off HEAD; uncommitted local changes won't be in their world. Commit or stash.
- **`auth_mode = "api"` but no API key**: tell user to `export ANTHROPIC_API_KEY=...` and re-run setup (to push the Modal secret).
- **Throttle mid-run**: the governor handles it automatically — cap halves for 10 min. Surface to the user but don't panic.

---

## Subcommand reference (for your bash calls)

```
legion init <tasks.json>
legion run [--tick-seconds N] [--max-ticks M] [--quiet]
legion status
legion cap
legion scale <n|auto>
legion stop [--force]
legion cleanup [--all]
legion reconcile
legion mediate <task-id>
```

The `run` subcommand is the primary driver — everything else is for inspection or manual intervention.
