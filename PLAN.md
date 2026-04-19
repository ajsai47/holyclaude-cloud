# HolyClaude-Cloud — 10x Plan

**Status after Phase 4:** the swarm works end-to-end. `/legion-start "goal"` decomposes, dispatches, merges, ships PRs, resolves conflicts. Local + cloud workers, 3-task DAG validated in <1 min.

**But "works" ≠ "trustworthy at scale."** Polishing tick output and adding progress bars is 1.1x. This doc identifies the three structural gaps that, if closed, genuinely 10x the product.

---

## Where the leverage is

Three gaps cap the current value. Everything else is 1.2-1.5x.

### Gap 1 — Decomposition is a one-shot guess

Goal → one Opus call → tasks.json → run. No critique, no iteration, no visibility into whether the decomposition is actually good.

The decomposition is the **single highest-leverage step** in the pipeline: bad graph means five workers each burn five minutes on the wrong thing. Yet we spend less compute on the decomposition than on one worker's task. Asymmetric in the wrong direction.

### Gap 2 — The swarm ships to main with no review

Worker finishes → reconciler merges. HolyClaude has `/review`, `/security-review`, and a dozen review agents — none of them are used on legion PRs. Every PR lands with exactly one set of eyes (the worker's own).

For toy repos this is fine. For real codebases — production code, security-sensitive paths, compliance boundaries — it's unshippable. **This is the blocker to using holyclaude-cloud on anything that matters.**

### Gap 3 — The shared brain exists but doesn't learn

Workers technically mount a shared `claude-mem` Modal Volume, but:
- Workers don't actively query it before starting a task
- The decomposer never consults it
- No structured retrospectives are written after tasks complete

The "compounding growth" thesis of meta-on-meta is currently just **file sharing**. No actual learning. Run 100 times on the same project, get the same quality on run 100 as on run 1.

---

## The three moves

### Move 1 — Decomposition as a subgraph, not a subroutine

Replace the single Opus call with a mini-swarm:

```
  goal ──► Decomposer ──► tasks.json
                           │
                           ▼
                       Critic ◄──── scans repo (tree-sitter + grep)
                           │         + queries shared brain for
                           │           similar past goals
                           │
            flags: overlapping files_touched,
                   missing tests, vague specs,
                   dep cycles, cost estimates
                           │
                           ▼
                       Refiner ──► revised tasks.json
                           │
                     iterate ≤ 3x
                           │
                           ▼
                  user checkpoint
```

Adds 30-60s of orchestration time. Saves 2-3x that by catching bad decompositions before 5 workers each burn 5 min on the wrong thing. Also predicts conflicts → pre-serializes conflicting siblings (adds dep edges), so the mediator barely runs.

**Bundled with this move: model-tier routing.** Router picks not just `local/cloud` but also Haiku/Sonnet/Opus. Trivial tasks → Haiku (~$0.001/task). Standard → Sonnet (default). Architecturally complex → Opus. ~5-10x cost reduction on mixed workloads with no quality loss on hard tasks.

### Move 2 — Pre-merge review gate

Before the reconciler merges, spawn a **reviewer worker** per PR:

- Loads HolyClaude's `/review` + `/security-review` skills
- Checks for: security issues (SQL injection, XSS, secret leaks), structural problems (silent failures, type design, dead code), task-adherence (did the diff actually match the spec?)
- Outcomes:
  - **Reviews critical** → comment on PR, mark `merge_blocker = "review_failed"`, re-dispatch the task with review feedback appended to spec
  - **Reviews clean** → merge proceeds

Reviewer runs in parallel with next-task dispatch, so wall-clock cost is near-zero for a reasonably-sized DAG.

**This is the layer that takes the swarm from "impressive demo" to "trustworthy on my actual repos."**

**Bundled with this move: `legion watch` live TUI.** Top-level dashboard showing worker progress, per-task transcripts streaming, cost running total, DAG visualization, pause/resume individual tasks. Today users stare at `[tick 47] in_flight=3 ready=0 shipped=2 merged=2`; tomorrow they see actual work happening.

### Move 3 — Brain that actually learns

Every completed task writes a structured retrospective to the shared brain:

```json
{
  "goal_hash": "sha256 of the original goal",
  "task_spec_summary": "add typed util module",
  "files_touched": ["utils/nums.py"],
  "approach_summary": "created module with 2 functions + docstrings",
  "outcome": "shipped",
  "worker_minutes": 3.2,
  "ci_failed_first_try": false,
  "retry_count": 0,
  "mediator_needed": false,
  "review_issues": [],
  "lessons": "1-2 sentences the worker wrote about what mattered"
}
```

The **decomposer queries the brain first**: "similar goals attempted in this repo? what decomposition worked? what didn't?" Primes the Opus call with real learned context from past runs.

Every **worker queries the brain before starting its task**: "similar tasks in this repo? what approach worked? what files tripped previous workers up?"

After ~20 runs, this is qualitatively different from one-shot. The system starts knowing things about this codebase and this engineer's style that no human has ever written down.

**Bundled with this move: `legion learn` CLI.** Introspection into the brain: `legion learn recent`, `legion learn similar "<query>"`, `legion learn export > project-memory.md`. Lets users see and audit what the swarm has learned about their repo.

---

## Suggested order

| # | Move | Why here |
|---|---|---|
| 1 | **5b — Pre-merge review gate** | Unblocks using this on real repos *this week*. Highest ROI on trust. |
| 2 | **5a — Decomposition subgraph** | Biggest speed + correctness win. Compounds on top of 5b (better decompositions = fewer review iterations). |
| 3 | **5c — Learning brain** | Longest time-to-value, highest ceiling. Compounds on top of both. |

Rationale for this order:
- **5b first** because it's the blocker to real-world usage. Everything else is optimization of a system you can't use yet.
- **5a second** because good decomposition makes review cheap (tasks that match their spec pass review faster).
- **5c last** because the brain's value comes from volume of data — it needs 5a and 5b shipped first so the retrospectives it records are high-quality.

---

## Non-goals (this phase)

Things we could build but aren't on the critical path to 10x:

- Real Modal billing API integration (Pro session = $0; matters only when `auth_mode=api`)
- Per-worker repo cache / shared bare clone (optimization at 20+ worker scale)
- claude-peers integration (nice-to-have; doesn't unblock)
- Resume mid-task (already supported at task boundaries via `/legion-start --resume`)
- Hierarchical decomposition / nested DAGs (captured by iterative decomposition in 5a with less complexity)
- First-class autoloop preset support (orthogonal feature, can be bolted on later)

These may ship later, but none of them individually changes the character of the product.

---

## Success criteria

Per move:

### 5b (review gate) — shipped when:
- [ ] A PR with a deliberately-inserted SQL injection vulnerability is caught and blocked from merge
- [ ] A PR whose diff doesn't match its task spec is caught and re-dispatched
- [ ] Clean PRs merge normally with no visible overhead
- [ ] `legion watch` shows reviewer activity live in the TUI

### 5a (decomposition subgraph) — shipped when:
- [ ] A 5-task decomposition with a predicted conflict gets the conflict flagged by the critic and an extra dep added before dispatch
- [ ] Average decomposition quality (judged by: fewer mediations needed + fewer review re-dispatches) improves measurably over a 10-run baseline
- [ ] Model-tier routing cuts average $ per run by ≥3x on mixed workloads

### 5c (learning brain) — shipped when:
- [ ] Running the same goal twice in the same repo produces measurably different/better behavior the second time
- [ ] `legion learn export` produces a readable project-memory doc worth sharing with human teammates
- [ ] After 20 runs on one repo, decomposition latency drops (because the brain has primed common patterns)

---

## What "10x" means here

"Works" → "trustworthy at scale." Specifically:

1. **You can run this on your actual employer's codebase without breaking production.** (Today: no. After 5b: yes.)
2. **Decomposition is faster and more correct, using cheaper models where appropriate.** (Today: one-shot Opus for everything. After 5a: Haiku for triage, Sonnet for most, Opus for hard calls, critic loop catches mistakes.)
3. **The system gets smarter the more you use it, compounding across runs.** (Today: stateless at the run boundary. After 5c: every run leaves the swarm more capable on this specific project.)

Each of the three moves is independently valuable. Together, they shift the product from "impressive demo" to "a second engineer who learns your codebase."
