<div align="center">

```
 ██╗  ██╗ ██████╗ ██╗  ██╗   ██╗ ██████╗██╗      █████╗ ██╗   ██╗██████╗ ███████╗
 ██║  ██║██╔═══██╗██║  ╚██╗ ██╔╝██╔════╝██║     ██╔══██╗██║   ██║██╔══██╗██╔════╝
 ███████║██║   ██║██║   ╚████╔╝ ██║     ██║     ███████║██║   ██║██║  ██║█████╗
 ██╔══██║██║   ██║██║    ╚██╔╝  ██║     ██║     ██╔══██║██║   ██║██║  ██║██╔══╝
 ██║  ██║╚██████╔╝███████╗██║   ╚██████╗███████╗██║  ██║╚██████╔╝██████╔╝███████╗
 ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝    ╚═════╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚══════╝
                              C  L  O  U  D
```

**You type a goal. A legion of Claude agents fans out, writes the code, reviews each other's work, and merges everything cleanly — while you do something else.**

</div>

---

## What it looks like in practice

```
/legion-start "Add JWT authentication to the API with tests and docs"

⚡ Decomposed into 4 tasks (2 parallel, estimated 18 min wall-clock)
⚡ T-001  Add JWT middleware to /api/user  →  cloud
⚡ T-002  Add /api/auth endpoint           →  cloud
⚡ T-003  Add auth integration tests       →  cloud (waiting on T-001)
⚡ T-004  Update README with auth section  →  local

✓ T-001  PR opened: github.com/you/repo/pull/12  (3 files, 89 lines)
✓ T-002  PR opened: github.com/you/repo/pull/13  (2 files, 45 lines)
↩ T-002  CI failed — re-dispatching (retry 1/2)
✓ T-002  PR opened: github.com/you/repo/pull/14  (CI passing this time)
✓ T-003  PR merged  ✅
✓ T-004  PR merged  ✅
✅ All 4 tasks merged in 22 minutes
```

One goal in. Merged PRs out. The orchestrator decomposed the work, routed tasks to cloud workers, an adversarial reviewer read every PR before merge, a CI failure triggered an automatic retry with the failure log as context, and the whole thing landed in dependency order.

## Status: v0.2.0 — alpha

End-to-end validated on its own repo ("dogfood") plus a sandbox. Ships PRs that merge cleanly when CI passes. Not yet tested on production codebases, protected branches, or repos with pre-commit hooks. See [STATUS.md](STATUS.md) for a grade card and [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for known edges.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  Layer 10: GOVERNOR     Cost cap, throttle observer, kill switch │
│  Layer 9:  RECONCILER   Dep-ordered merge, CI check, auto-heal   │
│  Layer 8:  REVIEWER     Adversarial pre-merge review + re-disp.  │
│  Layer 7:  MEDIATOR     Merge conflict resolution                │
│  Layer 6:  DISPATCHER   Local Agent vs cloud Modal — auto-route  │
│  Layer 5:  ORCHESTRATOR Decomposes plan → critic → refiner       │
├──────────────────────────────────────────────────────────────────┤
│  Layers 0-4: HolyClaude (memory, browser, plugins, workflow,     │
│              team, research) — peer dependency + baked into      │
│              every cloud container                               │
└──────────────────────────────────────────────────────────────────┘
```

**Orchestrator** takes your plain-English goal, turns it into a task DAG with explicit dependencies, and runs a critic-refiner pass to audit the plan before anything dispatches.

**Dispatcher** routes each task — local subprocess for quick tasks or repos with large checkouts, Modal cloud container for parallelism without saturating your machine.

**Reviewer** is a separate Claude instance that reads every PR before merge. A `critical` verdict blocks and re-dispatches the worker with the review as context. A `warnings` verdict merges with a comment. `clean` merges silently.

**Mediator** handles merge conflicts by spawning a dedicated Claude worker on a fresh worktree with both diffs and the conflict markers as input — it resolves by intent, not by guessing which diff wins.

**Reconciler** merges tasks in dependency order: T-002 doesn't merge until T-001 is in. CI failures trigger a re-dispatch with the failure log attached to the next worker's prompt.

**Governor** watches API spend and dynamically halves the worker cap if cost exceeds `max_dollars_per_hour` or the rate limiter fires.

## What makes this different

Most "AI coding tools" run one agent at a time. Legion runs N in parallel and coordinates them like a software team:

- **Parallel workers** that don't step on each other — each task gets its own git worktree and branch
- **Dependency ordering** so T-002 waits for T-001's PR to merge before it starts, not just before it runs
- **Adversarial review** — a separate Claude instance reads every PR before merge, not just the worker that wrote it
- **CI-aware re-dispatch** — if CI fails, the next worker gets the failure log as context and tries again with that information
- **Conflict mediation** — merge conflicts trigger a dedicated mediator agent with both diffs as context, resolving by intent
- **Cost cap** — `max_dollars_per_hour` kills the run if API spend exceeds budget; the governor detects 429s and backs off dynamically

## Requirements

- **macOS or Linux** — Windows untested
- **Python 3.11–3.13** — Python 3.14 has a known broken expat binding on macOS; use `brew install python@3.11` or pyenv if needed
- **[HolyClaude](https://github.com/ajsai47/holyclaude)** installed locally (peer dependency; baked into every cloud container)
- **[Claude Code](https://docs.anthropic.com/en/docs/claude-code)** CLI on PATH
- **[Modal](https://modal.com)** account + CLI (`pip install modal`) — used for cloud workers
- **[GitHub CLI](https://cli.github.com)** (`gh`) authenticated
- **Claude Pro subscription** (default auth) OR `ANTHROPIC_API_KEY` (pay-per-token escape hatch)

## Install

```bash
git clone https://github.com/ajsai47/holyclaude-cloud
cd holyclaude-cloud
pip install -e .      # puts `legion` on PATH
./setup               # pushes secrets to Modal, verifies auth
```

`./setup` checks Python version, finds your Modal CLI, extracts Claude Pro session creds from keychain, and pushes `GITHUB_TOKEN` as a Modal secret. Re-run it any time credentials rotate.

For a reproducible install pinned to the Modal worker image:

```bash
pip install -r requirements.txt
```

## Quickstart

Run from inside the target repo — the reconciler and reviewer need git context to call `gh pr merge`.

```bash
cd /path/to/your-target-repo
cp ~/holyclaude-cloud/config/legion.toml.example legion.toml
legion doctor          # verify all deps are healthy before the first run
```

Then in a Claude Code session inside that repo:

```
/legion-start "Add JWT auth to /api/user with tests and a README section"
```

The orchestrator decomposes the goal, shows you the task graph, waits for your go-ahead, then dispatches workers and narrates progress until everything is merged.

> **First run:** Modal builds the worker image on first use (~12 min, one time per Modal account). The image includes Node 20, Bun, Playwright, Claude Code CLI, HolyClaude, and `gh`. Subsequent runs start in seconds.

## Commands

| Command | Purpose |
|---|---|
| `legion doctor` | Verify all deps, auth, and Modal connectivity before running |
| `legion decompose "<goal>"` | Generate `.legion/tasks.json` from a plain-English goal using Claude |
| `/legion-start <goal>` | Decompose, dispatch, ship |
| `/legion-status [task-id]` | What's running right now |
| `/legion-scale <n \| auto>` | Override max_workers, or clear the override |
| `/legion-stop [--graceful \| --force]` | Stop the run |
| `/legion-cost` | Usage summary |
| `/legion-reconcile [--loop]` | Drain merge queue manually |
| `/claudecloud` | Lower-level — spin up one-off cloud tasks |

The `bin/legion` Python CLI is also available directly:

```
legion init <tasks.json>
legion decompose-refine <tasks.json> [--goal G] [--iterations N]
legion critique <tasks.json> [--goal G]
legion run [--tick-seconds N] [--max-ticks M] [--quiet]
legion review <task-id> | legion mediate <task-id>
legion cleanup [--all]
```

## What happens under the hood

**The fast path** (everything works, CI passes): the orchestrator decomposes your goal into a DAG, the critic validates it, and the dispatcher fans out workers. Each worker clones the repo into an isolated worktree, runs `claude -p` with a framed prompt, commits the diff, pushes a branch, and opens a PR. The reviewer reads it; if it's clean, the reconciler merges in dep order via `gh pr merge --squash`. You get merged PRs.

**When CI fails**: the reconciler catches the failure, attaches the CI log to the next worker's prompt as explicit context, and re-dispatches. The worker knows what broke and why. Up to `max_review_redispatches` retries.

**When the reviewer blocks**: a `critical` verdict means the diff had a real problem — the reviewer's notes become the next worker's starting context. A `warnings` verdict means minor issues — the PR merges with a comment attached.

**When there's a merge conflict**: the mediator spawns a Claude worker on a fresh worktree with both diffs and the full conflict markers. It resolves by intent (what each task was trying to accomplish), force-pushes the resolved branch, and retries the merge.

**When you're over budget**: the governor watches API spend per hour. If it exceeds `max_dollars_per_hour`, the run stops. If the rate limiter fires (429), it dynamically halves the worker cap for 10 minutes and resumes.

## Branch protection and needs_human

If your repo requires PR reviews before merging (branch protection rules, CODEOWNERS), legion opens PRs and runs CI but can't auto-merge them. Tasks reach `needs_human` status:

```
⚠ T-001: needs_human — approve and merge manually:
    https://github.com/owner/repo/pull/1
```

Options:

1. **Manual approval** — review and approve each PR, then run `legion run` again. The reconciler auto-heals and merges once approved.
2. **Branch protection exception** — add a protection exception for `legion/*` branches in GitHub repo settings.
3. **Bot account** — a second GitHub account as collaborator can approve legion's PRs. Set `GITHUB_TOKEN` to the bot's token before `./setup`.
4. **`--admin` merge** — if you're a repo admin: `gh pr merge <url> --squash --admin` bypasses protection for one-off runs.
5. **`use_admin_merge = true`** in `legion.toml` — if you're a repo admin, add this to bypass protection entirely. Legion will merge all PRs autonomously without human approval.

## Configuration

All settings live in `legion.toml` at the repo root (gitignored by default):

```toml
[swarm]
max_workers = 5                          # hard cap on concurrent workers
ramp_first_run = true                    # start at 1, +1 per ship
human_checkpoint_after_decompose = true  # pause before dispatch
auth_mode = "session"                    # "session" | "api"

[review]
enabled = true                           # adversarial review gate
max_review_redispatches = 2

[reconciler]
mediator_max_retries = 2                 # conflict resolution attempts
branch_prefix = "legion/"

[budget]
max_dollars_per_hour = 0                 # API-mode cost cap (0 = unlimited)
worker_timeout_minutes = 30              # stale-worker reaping
```

Full reference in [config/legion.toml.example](config/legion.toml.example).

## Auth model

Workers default to `auth_mode = "session"` — one Claude Pro session token shared across N parallel workers. This rate-limits above ~3–5 concurrent workers. The governor detects 429s and dynamically halves the cap for 10 minutes.

For serious workloads, flip to `auth_mode = "api"` in `legion.toml` and re-run setup with `ANTHROPIC_API_KEY` exported. No rate limits, but you pay per token.

**ToS note:** Running multiple headless Claudes on one Pro session at scale is grey-area commercial use. Personal projects only.

## What's not yet supported

What IS supported: parallel cloud workers, dependency-ordered merges, adversarial review with re-dispatch, CI-aware retry, merge conflict mediation, cost cap enforcement, and end-to-end runs on repos without branch protection or pre-commit hooks.

What isn't supported yet:

- **Branch protection with required reviewers / CODEOWNERS** — `gh pr merge` fails; tasks hit `needs_human`
- **Pre-commit hooks** — workers don't run `husky`/`pre-commit`; hook failures mean no PR opens
- **Large repos (1GB+)** — every cloud worker does a fresh clone; use `--target local` for big repos
- **Large diffs (>40KB)** — reviewer truncates to head + tail; large diffs get conservative `warnings` verdicts
- **Shared memory between workers** — the Modal volume is mounted but not yet read/written; "compounding growth" is Phase 5c, not shipped
- **CI re-dispatch on real CI failures** — the code path exists in the reconciler but hasn't been exercised end-to-end on a real CI failure yet

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for debugging workflows.

## License

MIT
