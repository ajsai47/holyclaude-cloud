# holyclaude-cloud — phase status

## Phase 1 — Single-worker happy path  ✅ shipped

- [x] Repo scaffold + plugin manifest
- [x] Meta-on-meta Modal image (`modal/image.py`)
- [x] Worker function (`modal/worker.py`) — clone → claude -p → push → PR
- [x] Setup script (auth + secrets + image build trigger)
- [x] Three commands: `/legion-start`, `/legion-status`, `/legion-stop`
- [x] `legion.toml.example`
- [x] `gotchas.md`

## Phase 2 — Multi-worker dispatch + governor  ✅ shipped

Real working code for concurrent dispatch, atomic state, throttle-aware cap.

- [x] `lib/state.py` — file-locked `.legion/state.json` with Task + RunState dataclasses
- [x] `lib/config.py` — `legion.toml` loader with sane defaults
- [x] `lib/routing.py` — rules-based local-vs-cloud routing (no LLM call)
- [x] `lib/governor.py` — throttle observer, ramp logic, cost cap (stub), stale-worker detection
- [x] `lib/dispatch.py` — uniform spawn/poll/kill over Modal FunctionCall + local subprocess
- [x] `lib/cli.py` — 10 subcommands: init, ready, route, spawn, poll, status, scale, cost, stop, cap
- [x] `bin/legion` — shell shim
- [x] Cloud worker writes `result.json` to cache volume (pollable from orchestrator)
- [x] Two new commands: `/legion-scale`, `/legion-cost`
- [x] Orchestrator skill rewritten around the CLI (real dispatch loop, not just protocol description)
- [x] Local worker: `git worktree` + subprocess `claude -p` + commit/push/PR post-exit
- [x] End-to-end smoke test on fake 3-task graph: init → ready → route → status → scale → cap → stop → cost all pass

## Phase 3 — Reconciler + mediator  ✅ shipped

- [x] `lib/reconciler.py` — `ready_to_merge`, `check_ci`, `merge_pr`,
      `wait_for_mergeable`, `auto_heal` (stale-state self-repair)
- [x] `lib/mediator.py` — fresh worktree off main, forced merge to expose
      conflict markers, framed prompt (preserve both intents), claude -p,
      post-exit commit + force-push to task branch
- [x] Dependency-ordered merge via `ready_to_merge` (only merges tasks
      whose deps are all merged)
- [x] CI integration via `gh pr checks` — pending/fail/pass/none
- [x] Conflict detection + mediator invocation + retry merge
- [x] `wait_for_mergeable` polling (solves the UNKNOWN-state-after-force-push
      race with GitHub)
- [x] `mediator_attempts` tracked; retry cap via `legion.toml`
- [x] Benign gh_error recovery (local branch delete with active worktree)
- [x] Auto-heal: stale blockers cleared when PR is actually merged on GitHub
- [x] New CLI: `legion reconcile`, `legion mediate <task-id>`
- [x] New command: `/legion-reconcile [--loop]`
- [x] New skill: `skills/reconciler/SKILL.md`
- [x] **Live-tested conflict resolution:** two tasks edited the same README
      lines with incompatible changes; one merged, the second hit conflict,
      the mediator synthesized both intents ("apples & bananas"),
      force-pushed, retry merged. Main has the combined result.

## Phase 3b — deferred to Phase 4

- [ ] CI re-dispatch on test failure (put failing task back in the queue
      with the failure log as new context)
- [ ] Resume support: `/legion-start --resume` reads existing
      `.legion/state.json` and continues mid-run

## Phase 4 — Polish

- [ ] `--api` escape hatch for workers (when Pro session won't survive the load)
- [ ] Real Modal billing API integration in `legion cost`
- [ ] Per-worker repo cache (shared bare clone, `git clone --reference` per task)
- [ ] `claude-peers` integration so other Claude Code sessions see in-flight legion
- [ ] HolyClaude pin upgrade workflow (`./setup --upgrade-holyclaude`)
- [ ] First-class autoloop integration (legion-start can take an autoloop preset as input)
- [ ] "Clear override" for `legion scale` so it returns to ramp/throttle-only logic

## Known sharp edges (from gotchas.md)

- Pro session shared across N workers will rate-limit. Governor halves the cap automatically on 429 but don't push past 3-5 concurrent.
- Image is ~2.5GB. First build is 10-15 min.
- Workers can't see uncommitted local changes. Push before starting.
- Phase 2 stops at "PR opened" — you merge manually until Reconciler ships in Phase 3.
- `legion scale <n>` doesn't kill in-flight workers when scaling down.
- `git worktree` for local workers means you can't use `.legion/worktrees/*` as ordinary subdirs.
- ToS at scale is grey area. Personal projects only.

## Architecture at a glance

```
User
  │
  │ /legion-start "goal"
  ▼
Orchestrator Skill  ──(subprocess)──►  bin/legion
  │                                      │
  │ decompose                            ├─ init   → .legion/state.json (flock'd)
  │ dispatch loop                        ├─ ready  → next tasks
  │ narrate progress                     ├─ route  → local | cloud
  │                                      ├─ spawn  → local subprocess OR modal run --detach
  │                                      ├─ poll   → reads Modal volume, subprocess exit
  │                                      ├─ cap    → governor-computed dynamic cap
  │                                      ├─ scale  → override
  │                                      ├─ stop   → STOP marker, optional kill
  │                                      └─ cost   → usage summary
  │
  ├─ local worker:   git worktree + claude -p  →  push branch → gh pr create
  └─ cloud worker:   modal run worker.py       →  (inside container)
                                                   clone repo, mount shared-brain,
                                                   claude -p with holyclaude loaded,
                                                   push branch → gh pr create →
                                                   write result.json to cache volume
```

Shared brain (Modal Volume `holyclaude-cloud-shared-brain`) — all cloud workers
mount claude-mem SQLite here. WAL mode. Reads concurrent; writes serialize.
Workers learn from each other across runs. That's the compounding growth.
