---
name: legion-reconciler
description: Merge shipped PRs in dependency order, invoking the mediator on conflicts. Used by the orchestrator after workers finish, or manually via /legion-reconcile. Runs one idempotent pass per invocation — call repeatedly while reconcile has work to do.
---

# legion-reconciler

Dependency-ordered PR merging. Mediator-aware.

---

## Flow per invocation

One pass:

1. `legion reconcile` finds tasks whose status == `shipped`, have a `pr_url`, have no `merge_blocker`, and whose deps are all `merged`.
2. For each:
   - **CI check** (`gh pr checks <pr-number>`):
     - `pending` → skip this tick, try again next pass
     - `fail` → mark `merge_blocker = "ci_failed"`, surface to user
     - `pass` or `none` → proceed
   - **Merge attempt** (`gh pr merge --squash --delete-branch`):
     - `merged` → mark `merged_at = now()`
     - `conflict` → invoke mediator (see below)
     - `gh_error` → mark `merge_blocker = "gh_error"`, surface
3. Emit a JSON list of actions taken.

The reconciler is idempotent. You can (and should) call `legion reconcile` repeatedly while there are shipped-but-unmerged tasks. In the orchestrator's main loop, interleave `legion poll` (worker status) with `legion reconcile` (PR status).

## Mediator fallback

On merge conflict:

1. Increment `task.mediator_attempts`.
2. If attempts > `config.reconciler.mediator_max_retries` (default 2), mark `merge_blocker = "mediator_maxed"` and stop.
3. Otherwise call `mediator.run_mediator(task, base_branch)`:
   - Creates a fresh worktree at `.legion/mediators/<task-id>/` off `origin/<base>`.
   - `git merge origin/<task.branch>` — forces conflict markers to appear.
   - Spawns `claude -p` with a mediator-framed prompt (preserve both intents, don't revert main's commits, don't touch unrelated files).
   - On clean exit + no remaining `<<<<<<<` markers: commit + force-push to `task.branch`.
   - Retry `gh pr merge` once.

If the mediator resolves the conflict and the retry merge succeeds: task status becomes `mediated_and_merged`.

If the mediator leaves conflicts or fails: `mediation_failed` with detail, task gets a blocker, surface to user.

## When to invoke from a skill

- **After orchestrator's dispatch loop finishes** — shipped tasks accumulate; drain them with reconcile.
- **Interleaved inside the dispatch loop** — as workers finish, merge them immediately so downstream deps can start.
- **Manually** — `/legion-reconcile` for a one-shot, `legion mediate <task-id>` to re-run the mediator on a specific task.

## Failure surfaces

- `merge_blocker = "ci_failed"`: user's CI is telling the task it's broken. Orchestrator should NOT re-dispatch automatically — the code is wrong, not the run. Surface to user with the PR URL + check detail.
- `merge_blocker = "mediator_maxed"`: the mediator tried `mediator_max_retries` times and couldn't resolve. Surface the PR + the mediator log (`.legion/mediator_logs/<task-id>.log`) and ask the user to resolve manually.
- `merge_blocker = "needs_human"`: reserved for Phase 4 — user explicitly escalated.
- `merge_blocker = "gh_error"`: gh CLI returned an error we don't know how to classify. Usually means auth, rate limit, or branch state issues. Show the raw error.

## Subcommands used

```
legion reconcile                # one idempotent pass
legion mediate <task-id>        # force mediator run on a task
legion status                   # shows merged/unmerged/blocked
```
