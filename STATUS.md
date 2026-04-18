# holyclaude-cloud — phase status

## Phase 1 — Single-worker happy path  ✅ scaffolded

Goal: `/legion-start "<goal>"` → orchestrator decomposes → ONE cloud worker spawns → clones repo, runs claude+holyclaude, pushes branch, opens PR.

Done:
- [x] Repo scaffold + plugin manifest
- [x] Meta-on-meta Modal image (`modal/image.py`)
- [x] Worker function (`modal/worker.py`) — clone → claude -p → push → PR
- [x] Setup script (auth + secrets + image build trigger)
- [x] Orchestrator skill (decompose + dispatch loop)
- [x] Dispatcher skill (rules-based local-vs-cloud routing)
- [x] Three core commands: `/legion-start`, `/legion-status`, `/legion-stop`
- [x] `legion.toml.example`
- [x] `gotchas.md` (Pro session at scale, shared brain, etc.)

Not done in Phase 1 (stubbed for Phase 2):
- [ ] Actual queue/state persistence (`lib/task_queue.ts` not implemented; orchestrator describes the protocol but doesn't have working persistence code yet)
- [ ] `/legion-scale` and `/legion-cost` commands
- [ ] Throttle observer
- [ ] Multi-worker dispatch (orchestrator skill describes it; not exercised)

## Phase 2 — Multi-worker + governor

- [ ] Real concurrent dispatch (track 5 in-flight workers)
- [ ] Governor skill: throttle observer, cost cap, ramp logic
- [ ] `/legion-scale <n>` command
- [ ] `/legion-cost` command (Modal billing API integration)
- [ ] State persistence in `.legion/state.json` with file lock

## Phase 3 — Reconciler + mediator

- [ ] Reconciler skill: PR dependency graph, ordered merges
- [ ] Mediator agent: resolve conflicts by reading both diffs and rewriting the loser
- [ ] Auto-merge when CI passes
- [ ] Re-dispatch on test failure

## Phase 4 — Polish

- [ ] `--api` escape hatch for workers (when Pro session won't survive the load)
- [ ] Per-worker repo cache (shared bare clone instead of N full clones)
- [ ] `claude-peers` integration so other Claude Code sessions on the machine see the in-flight legion
- [ ] HolyClaude pin upgrade workflow (`./setup --upgrade-holyclaude`)
- [ ] First-class autoloop integration (legion-start can take an autoloop preset as input)

## Known sharp edges (from gotchas.md)

- Pro session shared across N workers will rate-limit. Stay under 3-5 workers until you've got a feel.
- Image is ~2.5GB. First build is 10-15 min.
- Workers can't see uncommitted local changes. Push before starting.
- Phase 1 stops at "PR opened" — you merge manually until Reconciler ships in Phase 2.
- ToS at scale is grey area. Personal projects only.
