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

HolyClaude-Cloud is the orchestration layer for [HolyClaude](https://github.com/ajsai47/holyclaude). It spins up a swarm of Claude workers on Modal, each running the full HolyClaude stack (memory, browser, team review, autoloop), and coordinates them from a local orchestrator to ship massive coding projects in parallel.

```
┌──────────────────────────────────────────────────────────────────┐
│  Layer 10: GOVERNOR     Cost cap, throttle observer, kill switch │
│  Layer 9:  RECONCILER   PR ordering, conflict resolution, merge  │
│  Layer 8:  DISPATCHER   Local Agent vs cloud Modal — auto-route  │
│  Layer 7:  ORCHESTRATOR Decomposes plan → task queue             │
├──────────────────────────────────────────────────────────────────┤
│  Layers 0-6: HolyClaude (memory, browser, plugins, workflow,     │
│              team, research) — peer dependency                   │
└──────────────────────────────────────────────────────────────────┘
```

## Meta-on-meta

Each cloud worker is a Modal container with the **entire HolyClaude plugin** installed: same memory layer, same workflow skills, same team review agents. Workers share a single `claude-mem` SQLite database on a Modal Volume — they learn from each other across runs. That's the compounding growth.

## Quick start

```bash
git clone https://github.com/ajsai47/holyclaude-cloud.git
cd holyclaude-cloud
./setup
```

`./setup` will:
1. Verify HolyClaude is installed (peer dep).
2. Verify Modal CLI is configured.
3. Push your Claude Pro session token to Modal as the `claude-pro-session` secret.
4. Push your GitHub token to Modal as the `legion-github` secret.
5. Build (or rebuild) the meta-on-meta Modal image.
6. Drop a `legion.toml` in the current directory if missing.

## Commands

| Command | Purpose |
|---|---|
| `/legion-start <goal>` | Decompose, dispatch, ship |
| `/legion-status` | What's the swarm doing right now |
| `/legion-scale <n>` | Manual override to N workers |
| `/legion-stop [--graceful\|--force]` | Stop the swarm |
| `/legion-cost` | Today / this hour / this run |

## Defaults

```toml
# legion.toml
max_workers = 5
max_dollars_per_hour = 0          # 0 = Pro session only, no $ cap needed
human_checkpoint_after_decompose = true
ramp_first_run = true             # start with 1 worker, scale up after observing throttle
worker_timeout_minutes = 30
mediator_max_retries = 2
```

Set `human_checkpoint_after_decompose = false` for full-autonomous mode. Yolo coding mode.

## Auth model

All workers share **one Pro session token** (`claude-pro-session` Modal secret). This will throttle. The Governor watches for 429s and dynamically shrinks the swarm. ToS at scale is grey area — use this for personal projects, not commercial workloads.

## Status

Phase 1 (single-worker happy path) — in progress. See `STATUS.md`.

## License

MIT
