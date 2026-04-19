# holyclaude-cloud — phase status

---

## 🏷️ v0.1.0 — session handoff (2026-04-19)

Polished to a shareable tagged release. See [CHANGELOG.md](CHANGELOG.md) for the
release summary and [README.md](README.md) for install + quickstart.

### What's live

- Phases 1–4 core product
- Phase 5b review gate (calibrated on 3 scenarios + 1 adversarial test)
- Phase 5a v1 decomposition subgraph
- 2 successful dogfood rounds on this repo's own main (unit tests + CI)
- 4 PRs merged by legion into its own codebase
- `HOLYCLAUDE_REF` pinned to `b80d41f0cf39`

### Known untested paths (priority-ordered for next session)

1. **Phase 5c — learning brain.** Shared Modal volume is mounted but no code
   reads/writes retrospectives. Biggest structural gap from PLAN.md still open.
2. **CI re-dispatch end-to-end.** Code path exists (`cmd_reconcile` handles
   `ci_failed`). Never fired because all dogfood CI runs passed. Needs a task
   that produces failing tests to actually exercise the re-dispatch.
3. **`auth_mode = "api"`.** Setup pushes the secret; workers have the code
   path; never run end-to-end with a real key.
4. **`/legion-start --resume`.** Described in orchestrator skill; never tested
   against an actually-interrupted run.
5. **Branch-protected main.** `gh pr merge --squash` will fail; reconciler
   needs a "needs human review" blocker path.
6. **Pre-commit hooks in the target repo.** Worker has no awareness; `git
   commit` will fail if hooks reject the diff.

### Concrete first step for next session

Either:
- **Push into 5c** (biggest structural move; see [PLAN.md](PLAN.md))
- **OR deliberately trigger CI re-dispatch** (smallest bounded validation) —
  see Path B in end-of-session ranked recommendation
- **OR cross-repo dogfood** — run legion on a different one of your real
  projects to find what breaks on contact with real constraints (CLAUDE.md,
  CI, branch protection, codeowners)

My recommendation coming out of this session: run the cross-repo dogfood
first. It's the best predictor of "is this actually useful?" — and any
surprising breakage would reprioritize the Phase 5 roadmap.

---

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

## Phase 4 — autonomous runner + auth escape hatch + CI re-dispatch  ✅ shipped

- [x] `legion run` — single-command autonomous dispatch/poll/reconcile loop.
      Handles spawn, poll, reconcile, mediate, ci-redispatch, ramp, throttle
      backoff, stale-worker reaping, graceful stop. Exits when terminal.
- [x] Critical fix: `poll_local` completion signal. claude-code sometimes
      hangs after emitting its terminal `result` event. Was causing runs to
      get stuck indefinitely. Now reads the stream-json log for
      `"type":"result"` and kills lingering processes.
- [x] CI re-dispatch: tasks with `ci_failed` are put back as `pending` with
      the failing-check details appended to their spec. Capped by
      `mediator_max_retries`. The next worker sees what broke.
- [x] `--api` escape hatch: `[swarm] auth_mode = "api"` switches workers to
      `ANTHROPIC_API_KEY`. Modal secret `anthropic-api-key` is pushed by
      setup when the env var is set. Applies to local AND cloud workers.
- [x] Stale-worker reaping: workers past `worker_timeout_minutes` are
      force-killed and marked failed by `legion run` on each tick.
- [x] `legion scale auto` clears `max_workers_override`.
- [x] `/legion-start --resume` — skip decompose + init, continue from
      existing `.legion/state.json`.
- [x] Orchestrator skill rewritten around `legion run` — short, focused on
      decomposition + checkpoint + narration. The run loop owns everything
      else.
- [x] Live-validated: 3-task DAG (A, B independent; C depends on both)
      completed autonomously in 6 ticks. Spawn/ship/merge/dispatch-downstream
      all interleaved automatically. PRs #17, #18, #19 merged, final repo
      state correct (C imports from A and B).

## Phase 5b — Pre-merge review gate  ✅ shipped

See PLAN.md for the strategic framing.

- [x] `lib/reviewer.py` — adversarial reviewer worker: fetches PR diff,
      frames Claude with strict JSON verdict output, parses
      clean/warnings/critical + list of issues
- [x] Task state extensions: `review_verdict`, `review_issues`,
      `review_summary`, `review_attempts`
- [x] `[review]` config section in `legion.toml`: enabled, categories,
      max_review_redispatches, target
- [x] Reconciler hooks review between CI check and merge. Flow:
      - `critical` → re-dispatch task with issues appended to spec
        (up to `max_review_redispatches`); post PR comment with issue list
      - `warnings` → merge proceeds, issues posted as PR comment
      - `clean`    → merge silently
- [x] `legion review <task-id>` CLI subcommand for manual re-review
- [x] **Live-validated on deliberately-insecure task**: worker wrote
      `f"...WHERE username = '{username}'"` (real SQL injection) per a
      deliberately-bad spec. Reviewer caught it critical, re-dispatched
      with security feedback. Second worker produced parameterized query
      (safe). Reviewer on second attempt flagged spec-adherence warning
      but merged. Final merged code is safe despite unsafe spec.

Example reviewer comment from the live test (PR #20):
> SQL injection: username is interpolated directly into the query string via an f-string. Any caller-controlled username value can terminate the string literal and inject arbitrary SQL (e.g. ' OR '1'='1). Must use a parameterized query.

## Phase 5a — Decomposition subgraph  ✅ shipped (v1)

Critic + Refiner loop that audits proposed task DAGs before dispatch.
Catches obvious problems (file conflicts between siblings, dep cycles,
weak specs, oversized tasks) with deterministic Python checks PLUS
qualitative issues (ambiguous specs, scope drift, missing tests,
under-parallelization) via a Claude LLM call.

- [x] `lib/critic.py` — deterministic checks + LLM critique + refiner,
      combined into `critique_and_refine()` + `iterate_until_stable()`
- [x] CLI: `legion critique <tasks.json>` (read-only) and
      `legion decompose-refine <tasks.json>` (iterates + rewrites in place)
- [x] Orchestrator skill updated: inserts step 3a ("refine the
      decomposition") between decompose and init
- [x] Live-tested on a deliberately-bad DAG:
      - T-A + T-B both touch config.py with no dep → file_overlap flag
      - T-C has spec="fix it" → weak_spec + ambiguous_spec flags
      - T-C at 90 min → oversized + scope_drift flags
      - Refiner produced a clean 4-task DAG: serialized T-A→T-B, rescoped
        T-C to "wire flags to CLI", added T-D for tests.

Deferred to Phase 5a-v2:
- [ ] Model-tier routing (Haiku/Sonnet/Opus per task) — bundled with 5a
      in PLAN.md but separable; task complexity → model selection
- [ ] Codebase-aware critique — tree-sitter/grep-inform the critic about
      file existence, test conventions, style guides
- [ ] Brain-informed critique — query past retros via 5c (requires 5c first)

## Phase 5c — Learning brain

See PLAN.md. Structured retrospectives after every task, decomposer +
workers query the brain before starting.

## Deferred (lower priority)

- [ ] Real Modal billing API integration in `legion cost`
- [ ] Per-worker repo cache (shared bare clone, `git clone --reference`)
- [ ] `claude-peers` integration so other Claude Code sessions see the
      in-flight legion
- [ ] HolyClaude pin upgrade workflow (`./setup --upgrade-holyclaude`)
- [ ] First-class autoloop integration (`/legion-start --autoloop <preset>`)
- [ ] Per-task override target (`legion spawn <id> --target cloud` already
      works; want a one-shot way to re-route an in-flight task)

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
