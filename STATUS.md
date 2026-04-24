# holyclaude-cloud — phase status

---

## v0.3.0 — GA polish (2026-04-24)

### What shipped

**Bug fixes (were silent blockers):**
- `check_ci()` used nonexistent `conclusion` JSON field — always returned "none", making CI re-dispatch dead code. Fixed to use `bucket`/`state` fields from actual gh CLI output.
- Stale `.legion/blockers/<id>.md` from previous run caused re-dispatched worker to immediately fail. Fixed: CI re-dispatch path and `spawn_local` both clear it.
- 69/69 tests passing (1 was failing due to stale fixture using old `conclusion` field).

**New commands:**
- `legion doctor` — 7-check pre-flight: Claude Code CLI, gh auth, Modal CLI, both Modal secrets, legion.toml, git repo context.
- `legion decompose "<goal>"` — plain-English → tasks.json via Claude. Removes the need to write JSON manually.

**UX improvements:**
- Rich live terminal dashboard in `legion run`: narrated ⚡/✓/✅/↩/⚠ events + in-place task table with status icons, elapsed time, worker progress (last tool call shown for local workers). Falls back to plain text when piped.
- Rich Panel run summary (green/yellow/red border by outcome).
- Cloud auth 401 → actionable error: "Re-run ./setup to refresh credentials."
- `use_admin_merge = true` in legion.toml: fully autonomous merge, no needs_human on owned repos.
- Better PR descriptions: task title as heading, spec excerpt, files touched, styled footer.

**Dogfood results:**
- Dogfood #5: zero manual state edits, correct implementation, CI green.
- Dogfood #6: 4-task stats module (mean/median/stdev/exports), dep-ordered, all 4 PRs auto-merged via admin, zero intervention. Elapsed: ~6 minutes wall-clock.
- CI re-dispatch validated end-to-end: wrong hypotenuse impl → CI fail → reconcile → re-dispatch → correct impl → CI green.

### Known gaps (not blocking for alpha)
- Cloud session tokens expire; `./setup` required to refresh. Error message now actionable.
- `legion cost` returns $0 in session mode (Modal billing API not yet wired).
- Reviewer gate (enabled=false by default) works structurally but untested on `critical` verdicts in production.

---

## 🏷️ v0.2.0 — session handoff (2026-04-24)

Overnight session: 4 parallel agents + dogfood #4 on `ajsai47/legion-dogfood`.

### What shipped in v0.2.0

- **Packaging:** `pyproject.toml` with deps + entry point, `requirements.txt`, `pip install -e .` works, `legion` CLI available via entry point, modal path priority fixed in setup
- **Secret hygiene:** all `modal secret create` calls now use `--from-dotenv` + chmod-600 tmpfile; secrets never appear as CLI args or shell history
- **Worker hardening:** pre-commit hook detection → `--no-verify` + PR body note (#8 closed); `.mcp.json` renamed before `claude -p` + restored after (#11 closed)
- **CI re-dispatch tests:** 23 unit tests covering `check_ci()`, `fetch_ci_failure()`, re-queue state transition, retry cap
- **Setup UX:** missing GH token = hard exit; modal auth shows `modal token new`; final `[ok]/[-]` checklist
- **69 tests passing** (up from 46)

### Dogfood #4 — legion-dogfood cross-repo (2026-04-24)

Target: `ajsai47/legion-dogfood` (Python, branch protection + CODEOWNERS + CI guard required).
2-task DAG: T-001 `div()` + T-002 `mod()` (independent, both touch same test file).

**Outcome:** Both PRs merged. CI passing on both. Tasks were substantively correct.

**Findings (priority-ordered for next session):**

1. **NEW — Claude ignores "don't open PRs yourself" constraint.** T-001's worker
   instructed Claude not to open a PR; Claude opened one anyway with a different
   title. The worker's subsequent `gh pr create` hit "already exists" → returned
   `pushed_no_pr`, `pr_url: null`. State went out of sync with GitHub.
   Fix: worker should check `gh pr list --head <branch>` before `gh pr create`,
   and if a PR already exists adopt its URL rather than failing.

2. **NEW — Reconciler and reviewer require git-repo working directory.**
   Running `legion run` from a non-git directory (`/tmp/dogfood-run`) breaks
   `gh pr diff`, `gh pr merge`, and the local reviewer — all rely on git context.
   The intended pattern (run from inside the target repo) works; the non-repo
   pattern silently degrades. Fix: detect and error early, or allow passing
   `--repo-url` to reconciler so it can operate repo-agnostically.

3. **Branch protection + CODEOWNERS → needs_human path validated.** Reconciler
   correctly surfaces BLOCKED state. Can't auto-approve own PRs (GitHub security
   model). Manual admin merge with `gh pr merge --admin` works for dogfood.
   For production: needs a bot account or bypass token for the merge step.

4. **Mediator path exercised (manually).** T-001 and T-002 both edited
   `tests/test_math_ops.py` and `src/target/__init__.py` → real conflict on
   T-002 after T-001 merged. Resolved manually by rebase. Mediator code path
   works but requires git-repo context (same as finding #2).

5. **CI re-dispatch still not fired end-to-end.** All CI runs passed. The code
   path is unit-tested (23 tests) but never triggered in production.

6. **`local_file_threshold` routing trap.** Default threshold of 5 files routes
   small tasks local; local workers require a git-repo working directory. If the
   operator runs from a temp dir (as we did), every spawn fails silently.
   Fix: either document clearly (run from inside target repo) or detect non-git
   cwd and error before spawning.

### Revised priority list for next session

1. **Worker: adopt existing PR URL** (finding #1 — state sync bug, critical for reliability)
2. **Reconciler: repo-agnostic operation** (finding #2 — blocks reconcile from non-repo dirs)
3. **Operator docs / quickstart** — the "run from inside target repo" pattern is non-obvious; README needs a clear "where to run legion" section
4. **CI re-dispatch live validation** — synthesize a task that fails CI to exercise the path
5. **Bot account or bypass token for auto-merge** — branch protection wall requires this for fully autonomous operation
6. **Wire 5C brain** — deferred per cadence rule; unblock after findings #1+#2 closed

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

## 🧪 Dogfood #3 — ghost-v2 cross-repo run (2026-04-19 evening)

First run on a non-self repo. Target: `ajsai47/ghost-v2` (TypeScript
monorepo, bun + turbo + vitest + biome, no branch protection, real CI).
2-task DAG: add `Result<T,E>` to `packages/types` → refactor `parseLine`
in `packages/browser/src/snapshot/tree.ts` to consume it.

**Happy-path outcome after 3 false-starts:** 2 PRs shipped and merged in
6 min 25s of actual worker wall-clock.
[PR #1](https://github.com/ajsai47/ghost-v2/pull/1) ·
[PR #2](https://github.com/ajsai47/ghost-v2/pull/2).

### New findings (priority-ordered for next session)

1. **CRITICAL — dispatcher spawns on `shipped`, not `merged`.** T-002 was
   dispatched 26s before T-001 was merged into `main`. T-002's worker
   cloned `main` without T-001's `Result<T,E>` present, then
   re-implemented `result.ts`, `result.test.ts`, and `vitest.config.ts`
   from scratch. Merge succeeded only because both workers produced
   byte-identical code by coincidence — with any non-determinism
   (ordering, naming, style) this would have been a conflict or wrong
   behavior. Reviewer on PR #2 caught it: *"T-002 silently re-implements
   it"*. Fix: dispatcher's readiness check must wait for all deps in
   `merged` status, not just `shipped`.

2. **`worker.py` UnboundLocal in `auth_mode=api` branch.** `claude_dir`
   was defined only in the session-auth else branch (line 219) but
   referenced unconditionally at line 273 (`plugins_dir = claude_dir /
   "plugins"`). API auth path crashed immediately. **Fixed locally in
   `modal/worker.py`, uncommitted** — lifted `claude_dir` definition
   above the if/else. Awaiting review + commit.

3. **`legion run` summary miscounts terminal states.** First failure run
   printed `Failed: 0` despite `state.json` marking T-001 as
   `claude_failed`. The `claude_failed` status bypasses the `failed`
   tally in the summary printer. Mild — doesn't affect correctness, does
   affect operator trust ("why did this run end if nothing failed?").

4. **Session auth can't be auto-provisioned on newer Claude Code.**
   `setup` reads `~/.claude/.credentials.json`; that file no longer
   exists on this machine — session creds appear to have moved to macOS
   Keychain in a recent Claude Code version. Setup pushed whatever
   `claude-pro-session` secret was there previously, which had stale
   creds and returned 401 from Anthropic. Needs Keychain-read path in
   setup, OR clear error message + instructions when the creds file is
   missing.

5. **API auth path validated end-to-end** (closes Tier 1 item #3 from
   v0.1.0 handoff). Once a real `ANTHROPIC_API_KEY` is pushed to the
   `anthropic-api-key` Modal secret, workers authenticate and run
   without touching Pro session at all. `setup`'s placeholder-pushing
   behavior for the api secret when env is empty is correct — the
   worker's precondition check (`startswith("placeholder-")`) caught it
   cleanly.

6. **Review gate validated on real TypeScript.** Reviewer produced 4
   specific, correct `warnings`-tier issues across the 2 PRs:
   - PR #1: test file uses `as` casts instead of the `isOk`/`isErr`
     guards it's meant to validate
   - PR #1: `vitest.config.ts` sets `globals: true` AND test file
     imports `{ describe, expect, it }` — contradictory
   - PR #2: worker used only `!isOk`, never `isErr`, despite spec
     saying "use isOk/isErr for narrowing"
   - PR #2: re-implemented T-001's files (see finding #1)
   5b is not just "works" on TS — it's *productive*. Every warning was
   actionable.

7. **`.mcp.json` in target repo trips worker's claude-code init**
   (tangential). Transcript showed `{"name":"ghost-v2","status":"failed"}`
   during `system init`. Didn't block this run because T-001 succeeded
   anyway, but will bite repos that depend on their MCP server at
   runtime. Either: worker disables `.mcp.json` before `claude -p`, or
   adds MCP-server bootstrap to the preflight.

### Revised Tier 1 priority list for Session 2

Supersedes the list at the top of this file.

1. **[NEW] Dispatcher spawn-on-merged** (finding #1 — critical)
2. **[NEW] Commit the `claude_dir` fix** (finding #2)
3. **[NEW] `legion run` summary miscount** (finding #3 — small)
4. **Session auth** (finding #4 + original Tier 1 path #1)
5. **`/legion-start --resume`** (original Tier 1 path #4)
6. **Branch-protected main** (original Tier 1 path #5)
7. **Pre-commit hooks in target repo** (original Tier 1 path #6)
8. **CI re-dispatch end-to-end** (original Tier 1 path #2 — still open;
   this run had all CI runs pass, didn't fire the re-dispatch path)
9. **`.mcp.json` handling** (finding #7 — lower)

Dropped from list: `auth_mode="api"` end-to-end — closed by this run.

### Operational findings (for Tier 5 trust surface later)

- **Secret hygiene.** Re-pushing the API-key secret required
  `modal secret create anthropic-api-key ANTHROPIC_API_KEY=<value>` with
  the value on the command line. Value leaks into shell history + tool
  transcripts. `setup` should prefer stdin or env-only for secret
  creation. User should rotate keys that pass through this path.
- **Placeholder convention works.** The
  `placeholder-run-setup-with-ANTHROPIC_API_KEY-set-to-enable-api-mode`
  default value is a nice pattern — lets the Modal function decorator
  always attach the secret without failing deployment, and the worker's
  `startswith("placeholder-")` check produces a useful error rather
  than a cryptic 401.

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
