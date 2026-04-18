---
name: legion-start
description: Spin up a HolyClaude legion. Decompose a goal into tasks, dispatch local subagents and cloud Modal workers in parallel, ship PRs.
argument-hint: "<goal string OR path to plan/task file> [--yolo]"
---

# /legion-start

Start the swarm.

---

## Load skill

Load the `legion-orchestrator` skill from `~/holyclaude-cloud/skills/orchestrator/SKILL.md`. Read its full procedure before doing anything.

Also be aware of the `legion-dispatcher` skill at `~/holyclaude-cloud/skills/dispatcher/SKILL.md` — the orchestrator calls it per task.

---

## Parse arguments

$ARGUMENTS

- First non-flag arg = goal string OR path to a plan/task file.
- `--yolo` = override `human_checkpoint_after_decompose` to `false` for this run only.

If no goal was given, STOP and use AskUserQuestion: "What should the legion build? Paste a goal, a path to a /plan output, or a task list."

---

## Preflight

Before the orchestrator runs:

1. `git status --porcelain` — refuse to start if dirty (workers branch off HEAD; uncommitted changes won't be in their world). Tell the user to commit or stash.
2. `test -f ./legion.toml` — if missing, copy from `~/holyclaude-cloud/config/legion.toml.example` and tell the user the defaults.
3. Verify `~/holyclaude-cloud/setup` has been run (check that the `claude-pro-session` Modal secret exists via `/Users/ajsai47/tinker-env/bin/modal secret list`).

If any check fails, surface verbatim and stop. Don't paper over.

---

## Execute

Follow the orchestrator skill's 5-step procedure. Show progress narratively to the user — they should see:
- The decomposition (task graph).
- The dispatch decisions (per task: local or cloud, with one-line reason).
- Live status as workers complete (PR URLs as they open).

At the end, print:
- Total tasks: N
- Shipped (PR opened): N
- No changes: N
- Failed: N (with links to blocker docs)
- Total elapsed wall-clock
- Total parallel-worker-minutes consumed
- List of PR URLs

Reconciler is Phase 2 — for Phase 1, just hand the user the PR list and let them merge.
