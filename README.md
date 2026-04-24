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

**A legion of Claude workers, conducted from your terminal.**

</div>

---

Type `/legion-start "implement OAuth flow"` and walk away. A local orchestrator decomposes the goal into a task DAG, audits it with a critic, dispatches parallel workers to local subprocesses or Modal cloud containers, runs an adversarial reviewer on every PR, resolves merge conflicts with a mediator, and merges cleanly in dependency order. Each cloud worker runs the full [HolyClaude](https://github.com/ajsai47/holyclaude) stack — the compounding-growth thesis.

## Status: v0.1.0 — alpha

End-to-end validated on its own repo ("dogfood") plus a sandbox. Ships PRs that merge cleanly when CI passes. **Not yet tested on production codebases, protected branches, or repos with pre-commit hooks.** See [STATUS.md](STATUS.md) for an honest grade card and [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for known edges.

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

## Requirements

- **macOS or Linux**. Windows untested.
- **Python 3.11+** (`tomllib`, `dataclasses` with `list[str]` syntax)
- **[HolyClaude](https://github.com/ajsai47/holyclaude)** installed locally (peer dependency)
- **[Claude Code](https://docs.anthropic.com/en/docs/claude-code)** CLI on PATH
- **[Modal](https://modal.com)** account + CLI (`pip install modal`) — used for cloud workers
- **[GitHub CLI](https://cli.github.com)** (`gh`) authenticated
- **Claude Pro subscription** (default auth) OR `ANTHROPIC_API_KEY` (escape hatch; pay per token)

## Install

**Python 3.11–3.13 required.** Python 3.14 has a known broken expat binding on macOS. Use pyenv or `brew install python@3.11` if your system Python is 3.14+.

```bash
git clone https://github.com/ajsai47/holyclaude-cloud
cd holyclaude-cloud
pip install -e .      # puts `legion` on PATH
./setup               # pushes secrets to Modal, verifies auth
```

`setup` will check your Python version and exit with a clear message if it's wrong.  
Re-run `./setup` any time creds rotate.

**Reproducible install** (pins matching the Modal worker image):
```bash
pip install -r requirements.txt
```

The setup script:
1. Finds your Modal CLI (looks on PATH first, then common venv locations)
2. Verifies Modal auth — if not authed, run `modal token new` (opens a browser)
3. Extracts Claude Pro session creds from macOS keychain (or `~/.claude/.credentials.json`)
4. Reads `gh auth token` (or `$GITHUB_TOKEN`) and pushes as `legion-github` secret — **required**, exits if missing
5. Pushes a placeholder `anthropic-api-key` (the `--api` escape hatch needs the real key; see below)

## Quickstart

> **Where to run legion:** Always run `legion` from **inside the target repo's directory** (the repo you want workers to edit). The reconciler and reviewer need git context to call `gh pr merge`. Running from an unrelated directory will break reconcile.

```bash
cd /path/to/your-target-repo        # ← must be inside the repo
cp ~/holyclaude-cloud/config/legion.toml.example legion.toml
# edit legion.toml — set github_repo = "owner/repo"
```

Then in a Claude Code session inside that same repo:

```
/legion-start "Add JWT auth to /api/user with tests and a README section"
```

The orchestrator skill will:
1. Decompose the goal into tasks + run the critic/refiner to audit
2. Show you the task graph and ask to proceed (checkpoint)
3. Shell out to `~/holyclaude-cloud/bin/legion run` — the autonomous loop
4. Narrate progress; show you merged PRs at the end

## What happens under the hood

**First run** (of a repo): Modal builds the meta-on-meta image — Node 20, Bun, Playwright, the Claude Code CLI, HolyClaude at pinned `HOLYCLAUDE_REF`, gh CLI. ~10-15 minutes, one time. Subsequent runs reuse cached layers.

**Every run**:
- Local workers spawn as `subprocess.Popen` in `.legion/worktrees/<task-id>/`
- Cloud workers spawn via `modal run --detach` to `holyclaude-cloud-worker` app
- Each worker: clones the repo into a container, runs `claude -p` with a framed prompt, commits diff, pushes branch, opens PR via `gh`
- Reviewer runs on each shipped PR before merge — `critical` blocks + re-dispatches, `warnings` merges with PR comment, `clean` merges silently
- Reconciler merges in dep order via `gh pr merge --squash`
- Mediator kicks in on conflict — spawns a Claude worker on a fresh worktree with conflict markers, preserves both intents, force-pushes, retries merge

## Commands

| Command | Purpose |
|---|---|
| `/legion-start <goal>` | Decompose, dispatch, ship |
| `/legion-status [task-id]` | What's running right now |
| `/legion-scale <n \| auto>` | Override max_workers, or clear the override |
| `/legion-stop [--graceful \| --force]` | Stop the run |
| `/legion-cost` | Usage summary |
| `/legion-reconcile [--loop]` | Drain merge queue manually |
| `/claudecloud` | Lower-level — spin up one-off cloud tasks |

Under the hood, `bin/legion` exposes the Python CLI directly:
```
legion init <tasks.json>
legion decompose-refine <tasks.json> [--goal G] [--iterations N]
legion critique <tasks.json> [--goal G]
legion run [--tick-seconds N] [--max-ticks M] [--quiet]
legion review <task-id> | legion mediate <task-id>
legion cleanup [--all]
```

## Auth model

Workers default to `auth_mode = "session"` — one Claude Pro session token shared across N parallel workers. **This will rate-limit** above ~3-5 concurrent workers. The governor detects 429s and dynamically halves the cap for 10 min.

For serious workloads, flip to `auth_mode = "api"` in `legion.toml` and re-run setup with `ANTHROPIC_API_KEY` exported. No rate limits, but you pay per token.

**ToS note:** Running multiple headless Claudes on one Pro session at scale is grey-area commercial use. Personal projects only.

## Configuration

All settings live in `legion.toml` at the repo root (gitignored by default). Key knobs:

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
max_dollars_per_hour = 0                 # API-mode cost cap
worker_timeout_minutes = 30              # stale-worker reaping
```

Full reference in [config/legion.toml.example](config/legion.toml.example).

## Known limitations

- **Branch protection**: reconciler hasn't been tested against required reviewers / codeowners. Expect `gh pr merge` failures.
- **Pre-commit hooks**: workers don't know about `husky`/`pre-commit`. Hook failures mean no PR opens.
- **Repo size**: every cloud worker does a fresh clone. 1GB+ repos slow this down significantly; `--target local` is faster for big repos.
- **Diff size**: reviewer truncates diffs >40KB to head + tail. Large diffs get conservative `warnings` verdicts.
- **Shared brain**: the Modal volume is mounted but no code currently reads/writes to it. "Compounding growth" is Phase 5c, not shipped.
- **CI re-dispatch**: the code path exists in the reconciler but hasn't been exercised end-to-end on real CI failures.

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for debugging workflows.

## License

MIT
