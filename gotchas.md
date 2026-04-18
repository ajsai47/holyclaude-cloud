# holyclaude-cloud — gotchas

Read before launching the legion. Extends `~/.claude/skills/claudecloud/gotchas.md` (Modal CLI, no-pipe rule, image cache layering, etc. — all still apply). What's new here is everything about *parallel* and *meta-on-meta*.

## Pro session at scale

### One token, N workers, hard rate limits
All workers share `claude-pro-session`. The Pro plan's `rateLimitTier` is per-account, not per-session. Five concurrent workers = ~5x the throughput of one local Claude Code, and you'll hit the rate limit fast.

Symptoms:
- Workers' transcripts contain `429` or "rate limit exceeded".
- Workers exit with `claude_failed` after a few seconds.
- Subsequent retries also 429 because the limit window is still open.

Mitigations:
- `ramp_first_run = true` — start with 1, only add more after observing clean runs.
- The Phase 2 Governor will halve `max_workers` for 10 min on first 429.
- For Phase 1, keep `max_workers ≤ 3` until you have a feel for your account's tolerance.
- If you regularly hit limits, switch some workers to API auth via the `--api` escape hatch (Phase 3 — not yet implemented).

### Refresh-token death spiral
The Pro session token in the secret is the access token + refresh token. The Claude Code CLI refreshes automatically when the access token expires (~8h). In a Modal container, the refreshed token writes to the container's ephemeral FS and dies with the container.

Implications:
- Long worker runs (>8h) will fail mid-task on token refresh, because the container can write but not persist the new token.
- If you re-run `~/holyclaude-cloud/setup` while workers are running, the secret rotates but in-flight containers still have the old creds — they keep working until the access token expires.
- For runs >8h, switch to API auth or split tasks smaller.

### ToS at scale
Anthropic's Pro ToS covers personal use. Running 5 headless Claudes in parallel, 24/7, doing commercial work, is squarely outside that. Use this for personal projects, learning, side projects. Don't build a business on it.

## Meta-on-meta

### Image size is non-trivial
The full HolyClaude install + Playwright + Node + Bun + tree-sitter parsers + Python tooling = ~2.5GB image. First build: 10-15 min. Subsequent builds: ~30s if only the worker.py layer changed.

If you bump `HOLYCLAUDE_REF` in `modal/image.py`, layer 6 invalidates and everything after it (Playwright reinstall, GitHub CLI install, worker code) rebuilds. Plan ref bumps for when you have ~5 min of patience.

### Shared brain — concurrent SQLite writes
All workers mount the same `holyclaude-cloud-shared-brain` Volume at `~/.claude-mem/`. claude-mem uses SQLite. Concurrent writes from N containers can corrupt the DB.

Mitigations baked in:
- The image enables WAL mode on the claude-mem SQLite DB at boot.
- Reads are concurrent-safe under WAL.
- Writes serialize through SQLite's busy timeout (default 5s in claude-mem).

If you see "database is locked" errors in worker logs:
- One worker is hammering writes (probably the ralph-loop — it's chatty).
- Increase the busy timeout in claude-mem's config, OR
- Disable shared brain for that run (set env var `CLAUDE_MEM_DB=:memory:` per worker).

### Worker can't see your local in-flight changes
Workers `git clone` from GitHub. Anything you've committed but not pushed = invisible to them. Anything you've staged = invisible. Anything you've changed locally but not committed = invisible.

`/legion-start` refuses to start on a dirty tree, but it does NOT enforce "pushed" — you can have local commits ahead of origin. Push before starting if you want workers to see them.

## GitHub reconciliation

### gh PRs from a token = the token's user
Workers open PRs as whoever owns the `legion-github` token. If that's a bot account, PRs come from the bot. If it's your personal token, PRs come from you. Either way, the PR author isn't "claude" — pick your poison.

### Force-with-lease, not force
The worker pushes with `--force-with-lease` so a re-run of the same task-id won't blow away unrelated commits, but WILL overwrite a previous worker run on the same branch. Different task-ids = different branch names = no collision.

### Concurrent PRs don't auto-merge in Phase 1
Phase 1 stops at "PRs opened". You merge them manually. The Reconciler (Phase 2) handles dependency-ordered merges and conflict mediation.

If you merge PRs in dependency order yourself, expect manual resolution of any cross-task conflicts. Workers don't know about each other's branches at clone time.

### Branch litter
Every task spawns a `legion/T-NNN` branch. After a 50-task run, you have 50 dangling branches. Add a periodic cleanup or set GitHub's "auto-delete branches on merge" repo setting.

## Worktrees and atomicity

### Each worker clones the full repo
For a 1GB monorepo, 5 workers = 5GB of disk, 5 clone times. The shared-brain Volume DOES NOT include the repo — it's claude-mem state only. Future optimization: share a `worker-cache` Volume across workers with a shared bare-clone, and each worker `git clone --reference` from it.

### Idempotency on task-id
Re-running the same `task-id` reuses the cached worktree at `/workspace/repo` (if the container instance is still warm). It refreshes from `origin/<base-branch>` and recreates the branch. Net effect: a re-run of a task should produce the same PR (force-pushed) without leaking state from the first attempt.

But the task-id is your responsibility — if the orchestrator re-decomposes the goal and reassigns IDs differently, you'll get duplicate work on different branches.

## Debugging a failed worker

A cloud worker exited `claude_failed` or `no_changes`. Where to look:

1. `.legion/transcripts/T-NNN.jsonl` — pulled automatically from the worker-cache Volume after the run.
2. `modal app logs holyclaude-cloud-worker` — full container logs.
3. `modal volume get holyclaude-cloud-worker-cache T-NNN/log.txt -` — the worker's own step log.
4. `.legion/blockers/T-NNN.md` — if the worker explicitly gave up, this is its reasoning.

If the worker silently produced no diff: usually the prompt was underspecified, or claude-mem retrieved misleading context from a previous unrelated run. Re-decompose with more detail.
