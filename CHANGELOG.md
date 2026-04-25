# CHANGELOG

All notable changes to holyclaude-cloud.

## [v0.4.0] — 2026-04-24

### Phase 5c — Brain/learning loop

Workers now learn from experience across runs.

- **Write-on-finish**: every terminal task (merged, failed, no_changes, cancelled) writes a `Retro` to `~/.holyclaude-cloud/brain/` via `LocalFSBrainStore`. Retros include outcome, lessons, review issues, files touched, and CI failure flag. ([`1ab99f9`](https://github.com/ajsai47/holyclaude-cloud/commit/1ab99f9))
- **Read-on-spawn (local)**: orchestrator queries the brain store at spawn time, selects retros matching the repo + files, and injects a "Past experience" section into the worker prompt. Capped at 5 most-recent retros. ([`1ab99f9`](https://github.com/ajsai47/holyclaude-cloud/commit/1ab99f9))
- **Read-on-spawn (cloud)**: retros serialised as compact JSON, passed as `--retro-context` to `modal run`, parsed and injected inside the container — no Modal Volume round-trip required. ([`3eace9b`](https://github.com/ajsai47/holyclaude-cloud/commit/3eace9b))
- **Dedup guard**: `_brain_written_ids` set prevents double-writing on repeated ticks. ([`1ab99f9`](https://github.com/ajsai47/holyclaude-cloud/commit/1ab99f9))
- 8 unit tests in `tests/test_brain_wiring.py` covering retro injection, cap-at-5, review issue pass-through, in-flight skip, and dedup. ([`1ab99f9`](https://github.com/ajsai47/holyclaude-cloud/commit/1ab99f9))

### New-user polish (fixes that blocked first runs)

- **Git repo guard**: `legion init` and `legion run` now abort with a clear message if invoked outside a git repo. ([`4ddd849`](https://github.com/ajsai47/holyclaude-cloud/commit/4ddd849))
- **Session token expiry check**: `legion doctor` reads `claudeAiOauth.expiresAt` from Keychain (with fallback to `~/.claude/.credentials.json`) and warns if the token expires within 2 hours. ([`4ddd849`](https://github.com/ajsai47/holyclaude-cloud/commit/4ddd849))
- **Real cost tracking**: `legion cost` now parses `stream-json` logs for token events and reports actual input/output tokens + approximate dollar cost. ([`4ddd849`](https://github.com/ajsai47/holyclaude-cloud/commit/4ddd849))
- **Reviewer timeout safety**: `verdict == "error"` in the reconciler now `continue`s (skip-and-retry) instead of falling through to merge. ([`4ddd849`](https://github.com/ajsai47/holyclaude-cloud/commit/4ddd849))
- **Duplicate PR dedup**: both local and cloud dispatch now run a pre-flight `gh pr list --head <branch>` check before `gh pr create`, preventing desync when a PR already exists. ([`4ddd849`](https://github.com/ajsai47/holyclaude-cloud/commit/4ddd849))
- **`goal.txt` persistence**: `legion decompose` writes the plain-English goal to `.legion/goal.txt`; `legion run` reads it back so retros are tagged with the original goal. ([`4ddd849`](https://github.com/ajsai47/holyclaude-cloud/commit/4ddd849))

### UX polish

- **First-run cloud notice**: printed once on first Modal dispatch — tells the user about the ~12 min image build so they don't kill the process. ([`162c6f1`](https://github.com/ajsai47/holyclaude-cloud/commit/162c6f1))
- **Pre-commit hook detection**: `legion doctor` warns if `husky` or `.pre-commit-config.yaml` is found (workers don't run hooks). ([`162c6f1`](https://github.com/ajsai47/holyclaude-cloud/commit/162c6f1))
- **`.mcp.json` detection**: `legion doctor` warns if an `.mcp.json` is present at repo root; cloud worker renames it to `.mcp.json.legion-disabled` before running Claude to avoid conflicts. ([`162c6f1`](https://github.com/ajsai47/holyclaude-cloud/commit/162c6f1))
- **`needs_human` guidance**: upgraded from flat prose to bulleted causes + numbered instructions for enabling `use_admin_merge`. ([`162c6f1`](https://github.com/ajsai47/holyclaude-cloud/commit/162c6f1))
- **`cmd_status` shows `claude_failed`**: previously hidden; now surfaced in the failed group. ([`4ddd849`](https://github.com/ajsai47/holyclaude-cloud/commit/4ddd849))

### GA polish (earlier in this cycle, not in v0.1.0)

- Rich output (progress bars, colour tables) for `legion run`, `legion status`, `legion cost`
- `legion doctor` command — full pre-flight check (Python, Modal, gh, Claude Code, git, credentials)
- CI re-dispatch wiring: reconciler attaches CI log to re-dispatch prompt; validated end-to-end
- `use_admin_merge` toml key + `--admin` bypass
- `ramp_first_run` governor: starts at 1 concurrent worker, ramps +1 per shipped task
- Worker hardening: `.mcp.json` disable/restore, pre-commit hook detection inside container
- `legion decompose-refine` iterative critic+refiner CLI
- Packaging fixes: `pyproject.toml`, `requirements.txt`, `pip install -e .`
- Setup script improvements: Keychain-based session cred extraction, improved error messages

### Validation added in this release

- Brain loop: 8 unit tests (retro write, read-on-spawn injection, cloud retro pass-through, dedup)
- CI re-dispatch: end-to-end validated (failure → log attached → retry with context)
- Session token expiry check: manual validation on expired + valid credentials
- Duplicate PR dedup: validated on desync scenario where PR already existed

---

## [v0.1.0] — 2026-04-19

First tagged release. End-to-end validated via two dogfood rounds on the tool's own repo: [tests added by legion](https://github.com/ajsai47/holyclaude-cloud/pull/1), [CI added by legion](https://github.com/ajsai47/holyclaude-cloud/pull/4).

### Shipped phases

- **Phase 1** — Scaffold, plugin manifest, meta-on-meta Modal image (Node 20 + Bun + Playwright + Claude Code CLI + HolyClaude clone + gh CLI), single-worker happy path ([`543cc63`](https://github.com/ajsai47/holyclaude-cloud/commit/543cc63))
- **Phase 2** — Multi-worker dispatch, `legion` CLI (10 subcommands), governor (ramp + throttle + cost cap), file-locked state with atomic writes, routing rules (local-vs-cloud), setup script ([`ddccd43`](https://github.com/ajsai47/holyclaude-cloud/commit/ddccd43))
- **Phase 3** — Reconciler (dep-ordered `gh pr merge`), mediator (spawns Claude on fresh worktree with conflict markers, preserves both intents, force-pushes), auto-heal for stale state, `wait_for_mergeable` polling ([`e7013d6`](https://github.com/ajsai47/holyclaude-cloud/commit/e7013d6), [`3a09722`](https://github.com/ajsai47/holyclaude-cloud/commit/3a09722))
- **Phase 4** — `legion run` autonomous loop, CI re-dispatch on test failure, `--api` auth escape hatch, stale-worker reaping, `/legion-start --resume`, orchestrator skill rewritten around `legion run` ([`cdc95df`](https://github.com/ajsai47/holyclaude-cloud/commit/cdc95df))
- **Phase 5a v1** — Decomposition subgraph: deterministic + LLM critic, refiner loop up to N iterations, `legion critique` + `legion decompose-refine` CLI ([`54fba67`](https://github.com/ajsai47/holyclaude-cloud/commit/54fba67))
- **Phase 5b** — Pre-merge review gate: adversarial reviewer per PR, JSON verdict (clean/warnings/critical), PR comments, re-dispatch on critical up to `max_review_redispatches` ([`da3d262`](https://github.com/ajsai47/holyclaude-cloud/commit/da3d262), calibrated in [`633c1b5`](https://github.com/ajsai47/holyclaude-cloud/commit/633c1b5))

### Validation in this release

- 5 cloud worker runs (all clean after initial debug cycle)
- 8 local worker runs (all clean)
- Mediator validated on a 2-way semantic conflict (apples vs bananas)
- Reviewer validated on 3 calibration tests (clean code, subtle bug, large diff truncation) + 1 deliberately-unsafe SQL injection
- Critic+refiner validated on a deliberately-bad 3-task DAG (caught every issue)
- 4 PRs merged into the tool's own repo via legion
- 12 unit tests added by legion, passing in CI

### Key lessons (from the debug cycles)

- **Modal doesn't ship sibling `.py` files by default** — inline image definition into worker.py ([`6b5ccbc`](https://github.com/ajsai47/holyclaude-cloud/commit/6b5ccbc))
- **`gh auth login` wants a writable config file** — use env var + URL-embedded token instead ([`ac836f7`](https://github.com/ajsai47/holyclaude-cloud/commit/ac836f7))
- **`--dangerously-skip-permissions` refuses root UID** — set `IS_SANDBOX=1` ([`e069c34`](https://github.com/ajsai47/holyclaude-cloud/commit/e069c34))
- **`modal run --detach` is synchronous** — unified dispatch model via Popen + pid ([`af99a9a`](https://github.com/ajsai47/holyclaude-cloud/commit/af99a9a))
- **`claude-code` CLI hangs after emitting `"type":"result"`** — use log-based completion signal, not pid exit
- **Claude's `rate_limit_event` emits `status="allowed"`** — tighten throttle regex to avoid false positives
