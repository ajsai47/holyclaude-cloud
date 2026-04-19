# CHANGELOG

All notable changes to holyclaude-cloud.

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

### Known untested code paths

- CI re-dispatch (only happy-path CI has run; failure → redispatch never fired)
- `auth_mode = "api"` (secret pipeline exists, never run end-to-end)
- `/legion-start --resume` (described but never tested with an interrupted run)
- Branch-protected main (sandbox + tool repo both have no protection)
- Pro session throttle behavior under real load (governor code exists, never triggered)
- Shared-brain Modal volume (mounted, no workers query or write to it)

See [PLAN.md](PLAN.md) for the next structural moves (5c learning brain + 5a-v2 model-tier routing).

### Key lessons (from the debug cycles)

- **Modal doesn't ship sibling `.py` files by default** — inline image definition into worker.py ([`6b5ccbc`](https://github.com/ajsai47/holyclaude-cloud/commit/6b5ccbc))
- **`gh auth login` wants a writable config file** — use env var + URL-embedded token instead ([`ac836f7`](https://github.com/ajsai47/holyclaude-cloud/commit/ac836f7))
- **`--dangerously-skip-permissions` refuses root UID** — set `IS_SANDBOX=1` ([`e069c34`](https://github.com/ajsai47/holyclaude-cloud/commit/e069c34))
- **`modal run --detach` is synchronous** — unified dispatch model via Popen + pid ([`af99a9a`](https://github.com/ajsai47/holyclaude-cloud/commit/af99a9a))
- **`claude-code` CLI hangs after emitting `"type":"result"`** — use log-based completion signal, not pid exit
- **Claude's `rate_limit_event` emits `status="allowed"`** — tighten throttle regex to avoid false positives
