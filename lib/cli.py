"""legion CLI — the orchestrator skill's hands.

Subcommands are designed to be composable from a bash-driving orchestrator:

    legion init <tasks.json> --repo-url <url> --base-branch <br>
    legion route <task-id>
    legion spawn <task-id> [--target local|cloud]   # target required unless already decided
    legion poll                                     # updates state, prints changes
    legion ready                                    # prints next ready task IDs
    legion status                                   # human-readable table
    legion scale <n>                                # override max_workers
    legion cost                                     # cost summary (stub in Phase 2)
    legion stop [--graceful|--force]
    legion cap                                      # current dynamic max_workers
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from . import critic, dispatch, governor, mediator, reconciler, reviewer, routing, state
from .config import load as load_config


def _run_cmd(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout.strip()


# ----------------------------------------------------------------------
# init
# ----------------------------------------------------------------------

def cmd_init(args) -> int:
    """Create .legion/state.json from a tasks.json file."""
    tasks_path = Path(args.tasks)
    if not tasks_path.exists():
        print(f"error: {tasks_path} not found", file=sys.stderr)
        return 1

    raw = json.loads(tasks_path.read_text())
    if not isinstance(raw, list):
        print("error: tasks.json must be a JSON list of task objects", file=sys.stderr)
        return 1

    tasks = []
    for item in raw:
        tasks.append(state.Task(
            id=item["id"],
            title=item["title"],
            spec=item.get("spec", ""),
            deps=item.get("deps", []),
            estimated_minutes=item.get("estimated_minutes", 10),
            files_touched=item.get("files_touched", []),
        ))

    repo_url = args.repo_url or _run_cmd(["git", "remote", "get-url", "origin"])
    base_branch = args.base_branch or _run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"])

    run = state.init_run(repo_url, base_branch, tasks)
    print(json.dumps({
        "status": "initialized",
        "repo_url": run.repo_url,
        "base_branch": run.base_branch,
        "task_count": len(run.tasks),
    }, indent=2))
    return 0


# ----------------------------------------------------------------------
# ready
# ----------------------------------------------------------------------

def cmd_ready(_args) -> int:
    s = state.read_state()
    ready = state.ready_tasks(s)
    print(json.dumps([t.id for t in ready]))
    return 0


# ----------------------------------------------------------------------
# route
# ----------------------------------------------------------------------

def cmd_route(args) -> int:
    s = state.read_state()
    cfg = load_config()
    task = s.tasks.get(args.task_id)
    if not task:
        print(f"error: no task {args.task_id}", file=sys.stderr)
        return 1
    decision = routing.route(task, cfg.dispatch, governor.throttle_active(s))
    print(json.dumps({"target": decision.target, "reason": decision.reason}))
    return 0


# ----------------------------------------------------------------------
# spawn
# ----------------------------------------------------------------------

def cmd_spawn(args) -> int:
    s = state.read_state()
    cfg = load_config()
    task = s.tasks.get(args.task_id)
    if not task:
        print(f"error: no task {args.task_id}", file=sys.stderr)
        return 1
    if task.status == "in_flight":
        print(f"error: task {task.id} already in flight", file=sys.stderr)
        return 1

    # Resolve target
    target = args.target
    if not target:
        decision = routing.route(task, cfg.dispatch, governor.throttle_active(s))
        target = decision.target

    meta = dispatch.spawn(
        task,
        target=target,
        repo_url=s.repo_url,
        base_branch=s.base_branch,
        branch_prefix=cfg.reconciler.branch_prefix,
        auth_mode=cfg.swarm.auth_mode,
    )

    if not meta.get("worker_id"):
        print(json.dumps({"spawn_failed": meta}, indent=2), file=sys.stderr)
        return 2

    def mut(run: state.RunState):
        t = run.tasks[task.id]
        t.status = "in_flight"
        t.target = target
        t.worker_id = meta["worker_id"]
        t.branch = meta.get("branch")
        t.dispatched_at = meta.get("dispatched_at", time.time())
        run.events.append({
            "ts": time.time(),
            "kind": "spawn",
            "task_id": task.id,
            "target": target,
            "worker_id": meta["worker_id"],
        })

    state.update_state(mut)
    print(json.dumps({"task_id": task.id, **meta}, indent=2))
    return 0


# ----------------------------------------------------------------------
# poll
# ----------------------------------------------------------------------

def _pull_cloud_results(s: state.RunState) -> None:
    """Pull per-task result markers from the worker-cache volume to local."""
    cloud_in_flight = [t for t in s.tasks.values()
                       if t.status == "in_flight" and t.target == "cloud"]
    if not cloud_in_flight:
        return

    try:
        modal_bin = dispatch.find_modal_bin()
    except RuntimeError:
        return

    Path(".legion/cloud_results").mkdir(parents=True, exist_ok=True)
    for t in cloud_in_flight:
        local = Path(f".legion/cloud_results/{t.id}.json")
        # Best-effort — if the worker hasn't written the marker yet, this fails quietly.
        subprocess.run(
            [modal_bin, "volume", "get", "holyclaude-cloud-worker-cache",
             f"{t.id}/result.json", str(local), "--force"],
            capture_output=True,
        )


def _scan_for_throttle(s: state.RunState) -> bool:
    """Scan local + cloud logs for 429 patterns."""
    hit = False
    for t in s.tasks.values():
        if t.status != "in_flight":
            continue
        # Local log
        local_log = Path(f".legion/local_logs/{t.id}.log")
        if governor.scan_worker_log_for_throttle(local_log):
            hit = True
            break
        # Cloud result
        cloud_log = Path(f".legion/cloud_results/{t.id}.json")
        if governor.scan_worker_log_for_throttle(cloud_log):
            hit = True
            break
    return hit


def cmd_poll(_args) -> int:
    s = state.read_state()
    _pull_cloud_results(s)

    changes = []
    s2 = state.read_state()  # refresh after pulls

    # Detect throttle before processing status changes
    if _scan_for_throttle(s2) and not governor.throttle_active(s2):
        state.update_state(governor.record_throttle)
        changes.append({"throttle": "engaged"})

    in_flight = state.in_flight_tasks(s2)
    for task in in_flight:
        result = dispatch.poll(task, s2.base_branch)
        if result is None:
            continue
        new_status = result.get("status", "failed")
        changes.append({
            "task_id": task.id,
            "status": new_status,
            **{k: v for k, v in result.items() if k != "status"},
        })

        def mut(run: state.RunState, tid=task.id, res=result, new_status=new_status):
            t = run.tasks[tid]
            t.status = new_status
            t.finished_at = time.time()
            if res.get("pr_url"):
                t.pr_url = res["pr_url"]
            if res.get("error"):
                t.error = res["error"]
            run.events.append({
                "ts": time.time(),
                "kind": "finished",
                "task_id": tid,
                "status": new_status,
            })

        state.update_state(mut)

    # Flag stale in-flight
    cfg = load_config()
    stale = governor.stale_in_flight(state.read_state(), cfg)
    if stale:
        changes.append({"stale_in_flight": stale})

    print(json.dumps(changes, indent=2))
    return 0


# ----------------------------------------------------------------------
# status
# ----------------------------------------------------------------------

def cmd_status(_args) -> int:
    s = state.read_state()
    cfg = load_config()

    by_status: dict[str, list] = {}
    for t in s.tasks.values():
        by_status.setdefault(t.status, []).append(t)

    cap = governor.current_max_workers(s, cfg)
    throttle = "engaged" if governor.throttle_active(s) else "clean"
    elapsed = time.time() - s.started_at

    print(f"LEGION STATUS  —  repo: {s.repo_url}")
    print(f"  started: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(s.started_at))}")
    print(f"  elapsed: {int(elapsed // 60)}m {int(elapsed % 60)}s")
    print(f"  throttle: {throttle}   cap: {cap}/{cfg.swarm.max_workers}")
    print()

    def _print_group(header: str, tasks: list[state.Task]):
        if not tasks:
            return
        print(f"{header} ({len(tasks)}):")
        for t in tasks:
            extra = ""
            if t.pr_url:
                extra = f"  {t.pr_url}"
            elif t.target and t.worker_id:
                extra = f"  [{t.target}:{t.worker_id}]"
            if t.dispatched_at and t.status == "in_flight":
                in_flight_s = int(time.time() - t.dispatched_at)
                extra += f"  ({in_flight_s}s in flight)"
            if t.error:
                extra += f"  ERROR: {t.error[:80]}"
            print(f"  {t.id:8s} {t.target or '-':6s} {t.title[:60]:60s}{extra}")
        print()

    _print_group("In flight", by_status.get("in_flight", []))
    _print_group("Ready", state.ready_tasks(s))
    _print_group("Shipped", by_status.get("shipped", []))
    _print_group("No changes", by_status.get("no_changes", []))
    _print_group("Failed", by_status.get("failed", []))
    _print_group("Pending", [t for t in s.tasks.values() if t.status == "pending" and t.deps])

    return 0


# ----------------------------------------------------------------------
# scale
# ----------------------------------------------------------------------

def cmd_scale(args) -> int:
    # Support `legion scale auto` to clear the override
    raw = str(args.n).lower()
    if raw in ("auto", "clear", "none"):
        def mut(s: state.RunState):
            s.max_workers_override = None
            s.events.append({
                "ts": time.time(), "kind": "scale_cleared",
            })
        state.update_state(mut)
        print(json.dumps({"max_workers_override": None, "mode": "auto"}))
        return 0

    try:
        n = int(args.n)
    except (ValueError, TypeError):
        print(f"error: scale value must be an integer or 'auto', got {args.n!r}", file=sys.stderr)
        return 1

    def mut(s: state.RunState):
        s.max_workers_override = n
        s.events.append({
            "ts": time.time(),
            "kind": "scale",
            "max_workers_override": n,
        })
    state.update_state(mut)
    print(json.dumps({"max_workers_override": n}))
    return 0


# ----------------------------------------------------------------------
# cost
# ----------------------------------------------------------------------

def cmd_cost(_args) -> int:
    """Cost summary. Phase 2 stub — Modal billing API integration is Phase 4."""
    s = state.read_state()
    cfg = load_config()

    # Rough estimate: sum of in-flight time across cloud workers, assume $0
    # for Pro session workers. When we add --api, this becomes real.
    cloud_minutes = 0.0
    for t in s.tasks.values():
        if t.target != "cloud":
            continue
        if t.dispatched_at:
            end = t.finished_at or time.time()
            cloud_minutes += (end - t.dispatched_at) / 60.0

    summary = {
        "auth_mode": "pro_session",     # hardcoded for Phase 2
        "dollars_so_far": 0.0,          # Pro session is free
        "cloud_worker_minutes": round(cloud_minutes, 1),
        "cap_dollars_per_hour": cfg.budget.max_dollars_per_hour,
        "note": "Phase 4 will integrate real Modal billing API; for now Pro session = $0.",
    }
    print(json.dumps(summary, indent=2))
    return 0


# ----------------------------------------------------------------------
# stop
# ----------------------------------------------------------------------

def cmd_stop(args) -> int:
    mode = "force" if args.force else "graceful"
    state.write_stop(mode)

    if mode == "force":
        # Kill all in-flight workers
        s = state.read_state()
        killed = []
        for t in state.in_flight_tasks(s):
            if dispatch.kill(t, force=True):
                killed.append(t.id)

        def mut(run: state.RunState):
            for t in run.tasks.values():
                if t.status == "in_flight":
                    t.status = "failed"
                    t.error = "force-stopped"
                    t.finished_at = time.time()
            run.events.append({"ts": time.time(), "kind": "stop", "mode": "force"})
        state.update_state(mut)

        print(json.dumps({"stopped": "force", "killed": killed}))
    else:
        print(json.dumps({"stopped": "graceful"}))
    return 0


# ----------------------------------------------------------------------
# cap (how many workers we're allowed to have in flight right now)
# ----------------------------------------------------------------------

def cmd_cap(_args) -> int:
    s = state.read_state()
    cfg = load_config()
    cap = governor.current_max_workers(s, cfg)
    in_flight = len(state.in_flight_tasks(s))
    print(json.dumps({
        "current_cap": cap,
        "in_flight": in_flight,
        "slots_available": max(0, cap - in_flight),
        "throttle_active": governor.throttle_active(s),
        "stop_requested": state.stop_requested(),
    }))
    return 0


# ----------------------------------------------------------------------
# reconcile — merge shipped PRs in dep order, invoke mediator on conflict
# ----------------------------------------------------------------------

def cmd_reconcile(args) -> int:
    """One reconciliation pass. Idempotent — safe to call on a tick."""
    s = state.read_state()
    cfg = load_config()

    # Auto-heal stale state first: any "blocked" or "shipped but not merged"
    # task whose PR is actually merged on GitHub gets cleaned up.
    healed = reconciler.auto_heal(s)
    if healed:
        heal_ids = {h["task_id"] for h in healed}
        def _heal_mut(run: state.RunState, ids=heal_ids):
            now = time.time()
            for tid in ids:
                t = run.tasks[tid]
                t.merge_blocker = None
                t.error = None
                if t.merged_at is None:
                    t.merged_at = now
                run.events.append({
                    "ts": now, "kind": "auto_healed", "task_id": tid,
                })
        state.update_state(_heal_mut)
        s = state.read_state()

    ready = reconciler.ready_to_merge(s)
    results = []
    if healed:
        results.append({"auto_healed": healed})

    if not ready:
        print(json.dumps({"action": "idle", "ready_count": 0, "auto_healed": healed}, indent=2))
        return 0

    for task in ready:
        # 1. CI check
        ci = reconciler.check_ci(task.pr_url)
        if ci == "pending":
            results.append({"task_id": task.id, "status": "ci_pending"})
            continue
        if ci == "fail":
            # Re-dispatch: put the task back into pending with the CI failure
            # attached to its spec, so the next worker has context on what broke.
            if task.mediator_attempts < cfg.reconciler.mediator_max_retries:
                # Use mediator_attempts as overall retry counter — reusing it
                # avoids a new field; semantically it still caps "how many
                # times we auto-retry this task"
                failure_detail = reconciler.fetch_ci_failure(task.pr_url)
                def _mut(run: state.RunState, tid=task.id, det=failure_detail):
                    t = run.tasks[tid]
                    t.status = "pending"
                    t.mediator_attempts += 1
                    # Append CI failure to the task spec so the next worker sees it
                    t.spec = (
                        t.spec
                        + f"\n\n---\n## Previous attempt's CI failure (retry #{t.mediator_attempts})\n\n"
                        + det[:2000]
                    )
                    # Reset worker metadata so spawn treats it as fresh
                    t.worker_id = None
                    t.branch = None
                    t.pr_url = None
                    t.dispatched_at = None
                    t.finished_at = None
                    t.error = None
                    run.events.append({
                        "ts": time.time(), "kind": "ci_redispatch",
                        "task_id": tid, "attempt": t.mediator_attempts,
                    })
                state.update_state(_mut)
                results.append({
                    "task_id": task.id, "status": "ci_failed_redispatched",
                    "retry_number": task.mediator_attempts + 1,
                })
            else:
                def _mut(run: state.RunState, tid=task.id):
                    run.tasks[tid].merge_blocker = "ci_failed"
                    run.events.append({
                        "ts": time.time(), "kind": "merge_blocked",
                        "task_id": tid, "reason": "ci_failed",
                    })
                state.update_state(_mut)
                results.append({"task_id": task.id, "status": "ci_failed_max_retries"})
            continue

        # 1b. Pre-merge review gate (Phase 5b)
        if cfg.review.enabled and task.review_verdict != "clean" and task.review_verdict != "warnings":
            rv = reviewer.review_pr(task, cfg.review)
            verdict = rv.get("verdict", "error")

            if verdict == "error":
                # Reviewer itself failed — log and proceed to merge this tick.
                # Don't block on reviewer flakiness; next tick will retry.
                results.append({
                    "task_id": task.id,
                    "status": "review_errored",
                    "detail": rv.get("error", ""),
                    "log": rv.get("log", ""),
                })
                # Fall through to merge below

            elif verdict == "critical":
                # Block merge + potentially re-dispatch
                issues = rv.get("issues", [])
                summary = rv.get("summary", "")
                if task.review_attempts < cfg.review.max_review_redispatches:
                    # Re-dispatch with review feedback
                    feedback = reviewer.format_issues_for_spec(issues, summary)
                    def _redispatch(run: state.RunState, tid=task.id, fb=feedback,
                                    iss=issues, summ=summary):
                        t = run.tasks[tid]
                        t.status = "pending"
                        t.review_attempts += 1
                        t.review_verdict = None       # clear for re-review next time
                        t.review_issues = iss
                        t.review_summary = summ
                        t.spec = t.spec + fb
                        t.worker_id = None
                        t.branch = None
                        t.pr_url = None
                        t.dispatched_at = None
                        t.finished_at = None
                        t.error = None
                        run.events.append({
                            "ts": time.time(), "kind": "review_redispatch",
                            "task_id": tid, "attempt": t.review_attempts,
                            "issue_count": len(iss),
                        })
                    state.update_state(_redispatch)
                    # Also post a comment on the old PR explaining
                    if task.pr_url:
                        reviewer.post_pr_comment(
                            task.pr_url,
                            reviewer.format_issues_for_pr_comment(issues) +
                            "\n\n_Re-dispatching with this feedback as a new worker attempt._"
                        )
                    results.append({
                        "task_id": task.id, "status": "review_critical_redispatched",
                        "attempt": task.review_attempts + 1,
                        "issue_count": len(issues),
                    })
                    continue
                else:
                    def _block(run: state.RunState, tid=task.id, iss=issues, summ=summary):
                        t = run.tasks[tid]
                        t.merge_blocker = "review_failed"
                        t.review_verdict = "critical"
                        t.review_issues = iss
                        t.review_summary = summ
                        run.events.append({
                            "ts": time.time(), "kind": "merge_blocked",
                            "task_id": tid, "reason": "review_failed",
                        })
                    state.update_state(_block)
                    if task.pr_url:
                        reviewer.post_pr_comment(
                            task.pr_url,
                            reviewer.format_issues_for_pr_comment(issues) +
                            f"\n\n_Max review re-dispatches ({cfg.review.max_review_redispatches}) "
                            f"exhausted — needs human resolution._"
                        )
                    results.append({
                        "task_id": task.id, "status": "review_critical_maxed",
                        "issue_count": len(issues),
                    })
                    continue

            elif verdict == "warnings":
                # Merge proceeds, comment the warnings on the PR
                issues = rv.get("issues", [])
                summary = rv.get("summary", "")
                def _note(run: state.RunState, tid=task.id, iss=issues, summ=summary):
                    t = run.tasks[tid]
                    t.review_verdict = "warnings"
                    t.review_issues = iss
                    t.review_summary = summ
                state.update_state(_note)
                if issues and task.pr_url:
                    reviewer.post_pr_comment(
                        task.pr_url,
                        reviewer.format_issues_for_pr_comment(issues),
                    )
                # Fall through to merge

            else:  # clean
                def _ok(run: state.RunState, tid=task.id):
                    run.tasks[tid].review_verdict = "clean"
                state.update_state(_ok)
                # Fall through to merge

        # 2. Attempt merge
        mr = reconciler.merge_pr(task)
        if mr["status"] == "merged":
            def _mut(run: state.RunState, tid=task.id):
                run.tasks[tid].merged_at = time.time()
                run.events.append({
                    "ts": time.time(), "kind": "merged", "task_id": tid,
                })
            state.update_state(_mut)
            results.append({"task_id": task.id, "status": "merged"})
            continue

        if mr["status"] == "needs_human":
            # Branch-protected main or similar BLOCKED state. Mediator
            # can't help (there's no conflict to resolve) — stop here and
            # let a human approve/unblock the PR.
            def _mut(
                run: state.RunState,
                tid=task.id,
                err=mr.get("error", ""),
                merge_state=mr.get("merge_state", ""),
            ):
                run.tasks[tid].merge_blocker = "needs_human"
                run.tasks[tid].error = (err or "")[:500]
                run.events.append({
                    "ts": time.time(), "kind": "merge_blocked",
                    "task_id": tid, "reason": "branch_protection",
                    "merge_state": merge_state,
                })
            state.update_state(_mut)

            if task.pr_url:
                reviewer.post_pr_comment(
                    task.pr_url,
                    (
                        "### Legion merge blocked — needs human review\n\n"
                        "This PR is ready to merge but the base branch is "
                        "protected. Likely causes: required approvals from "
                        "CODEOWNERS, or a required status check that hasn't "
                        "passed.\n\n"
                        f"GitHub mergeStateStatus: `{mr.get('merge_state', 'UNKNOWN')}`\n\n"
                        f"gh error: `{(mr.get('error') or '').strip()[:300]}`\n\n"
                        "_Approve + merge manually, or resolve the blocker "
                        "and the next reconcile pass will auto-heal._"
                    ),
                )
            results.append({
                "task_id": task.id,
                "status": "needs_human",
                "merge_state": mr.get("merge_state"),
            })
            continue

        if mr["status"] == "conflict":
            # 3. Mediate
            if task.mediator_attempts >= cfg.reconciler.mediator_max_retries:
                def _mut(run: state.RunState, tid=task.id):
                    run.tasks[tid].merge_blocker = "mediator_maxed"
                state.update_state(_mut)
                results.append({"task_id": task.id, "status": "mediator_maxed"})
                continue

            med = mediator.run_mediator(task, s.base_branch)

            def _mut(run: state.RunState, tid=task.id):
                run.tasks[tid].mediator_attempts += 1
                run.events.append({
                    "ts": time.time(), "kind": "mediator_run",
                    "task_id": tid, "result": med.get("status"),
                })
            state.update_state(_mut)

            if med["status"] in ("resolved", "no_conflict"):
                # Let GitHub recompute mergeability after the force-push.
                # Without this, mergeable is UNKNOWN and retry immediately
                # fails with "not mergeable".
                reconciler.wait_for_mergeable(task.pr_url, timeout_s=45)
                # Retry merge
                mr2 = reconciler.merge_pr(task)
                if mr2["status"] == "merged":
                    def _mut(run: state.RunState, tid=task.id):
                        run.tasks[tid].merged_at = time.time()
                        run.events.append({
                            "ts": time.time(), "kind": "merged",
                            "task_id": tid, "via": "mediator",
                        })
                    state.update_state(_mut)
                    results.append({"task_id": task.id, "status": "mediated_and_merged"})
                else:
                    results.append({
                        "task_id": task.id,
                        "status": "mediation_ok_merge_failed",
                        "merge_detail": mr2,
                    })
            else:
                results.append({
                    "task_id": task.id,
                    "status": "mediation_failed",
                    "detail": med,
                })
            continue

        # 4. Other merge failure — not sticky. Next reconcile tick will retry,
        # which runs the _pr_is_merged upfront check to auto-heal if the PR
        # actually merged on GitHub's side.
        def _mut(run: state.RunState, tid=task.id, err=mr.get("error", "")):
            run.tasks[tid].error = err[:500]
        state.update_state(_mut)
        results.append({
            "task_id": task.id,
            "status": mr["status"],
            "will_retry": True,
            "error": mr.get("error", "")[:200],
        })

    print(json.dumps(results, indent=2))
    return 0


# ----------------------------------------------------------------------
# mediate <task-id> — manually invoke the mediator
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# review <task-id> — manually invoke the reviewer
# ----------------------------------------------------------------------

def cmd_review(args) -> int:
    s = state.read_state()
    cfg = load_config()
    task = s.tasks.get(args.task_id)
    if not task:
        print(f"error: no task {args.task_id}", file=sys.stderr)
        return 1
    if not task.pr_url:
        print(f"error: task {task.id} has no PR to review", file=sys.stderr)
        return 1
    rv = reviewer.review_pr(task, cfg.review)
    print(json.dumps(rv, indent=2))
    return 0 if rv.get("verdict") in ("clean", "warnings") else 2


def cmd_mediate(args) -> int:
    s = state.read_state()
    task = s.tasks.get(args.task_id)
    if not task:
        print(f"error: no task {args.task_id}", file=sys.stderr)
        return 1
    if not task.branch or not task.pr_url:
        print(f"error: task {task.id} has no branch/PR to mediate", file=sys.stderr)
        return 1
    result = mediator.run_mediator(task, s.base_branch)
    def _mut(run: state.RunState, tid=task.id):
        run.tasks[tid].mediator_attempts += 1
        run.events.append({
            "ts": time.time(), "kind": "mediator_run",
            "task_id": tid, "manual": True,
            "result": result.get("status"),
        })
    state.update_state(_mut)
    print(json.dumps(result, indent=2))
    return 0 if result.get("status") in ("resolved", "no_conflict") else 2


# ----------------------------------------------------------------------
# critique / refine — decomposition subgraph (Phase 5a)
# ----------------------------------------------------------------------

def cmd_critique(args) -> int:
    """Run deterministic + LLM critique on a tasks.json without modifying it."""
    tasks_path = Path(args.tasks)
    if not tasks_path.exists():
        print(f"error: {tasks_path} not found", file=sys.stderr)
        return 1
    tasks = json.loads(tasks_path.read_text())
    goal = args.goal or "(not provided)"
    result = critic.critique_and_refine(tasks, goal)
    print(json.dumps(result.to_dict(), indent=2))
    return 0


def cmd_decompose_refine(args) -> int:
    """Iterate critic + refiner up to --iterations times. Overwrites the tasks file."""
    tasks_path = Path(args.tasks)
    if not tasks_path.exists():
        print(f"error: {tasks_path} not found", file=sys.stderr)
        return 1
    tasks = json.loads(tasks_path.read_text())
    goal = args.goal or "(not provided)"

    if args.dry_run:
        result = critic.iterate_until_stable(tasks, goal, max_iterations=args.iterations)
        print(json.dumps(result.to_dict(), indent=2))
        return 0

    result = critic.iterate_until_stable(tasks, goal, max_iterations=args.iterations)
    if result.refined_tasks:
        # Backup the original
        backup = tasks_path.with_suffix(".json.pre-refine")
        backup.write_text(tasks_path.read_text())
        tasks_path.write_text(json.dumps(result.refined_tasks, indent=2))
        print(json.dumps({
            "status": "refined",
            "iterations": result.iterations,
            "remaining_flag_count": len(result.flags),
            "summary": result.llm_summary,
            "backup": str(backup),
        }, indent=2))
    else:
        print(json.dumps({
            "status": "stable" if not result.flags else "no_refinement",
            "iterations": result.iterations,
            "flag_count": len(result.flags),
            "summary": result.llm_summary,
        }, indent=2))
    return 0


# ----------------------------------------------------------------------
# run — autonomous dispatch + reconcile loop
# ----------------------------------------------------------------------

def render_run_summary(s: "state.RunState", say) -> int:
    """Print the final summary for a legion run and return an exit code.

    Every task is tallied into exactly one terminal category so the grand
    total matches len(s.tasks). Non-zero exit code is returned if any task
    ended in a worker/runtime failure, was blocked from merging, or was
    cancelled.

    Parameters
    ----------
    s: state.RunState
        The final run state to summarize.
    say: Callable[[str], None]
        Output sink (e.g. cmd_run's _say that respects --quiet).
    """
    tasks = list(s.tasks.values())

    merged = [t for t in tasks if t.status == "shipped" and t.merged_at is not None]
    # "shipped" but not merged and not otherwise blocked — these are PRs
    # still awaiting merge (e.g. run exited before reconciler landed them).
    shipped_open = [
        t for t in tasks
        if t.status == "shipped" and t.merged_at is None and not t.merge_blocker
    ]
    blocked = [t for t in tasks if t.merge_blocker is not None]
    failed = [
        t for t in tasks
        if t.status in ("failed", "claude_failed") and not t.merge_blocker
    ]
    no_changes = [t for t in tasks if t.status == "no_changes"]
    cancelled = [t for t in tasks if t.status == "cancelled"]

    # Anything not in a terminal state above (pending / ready / in_flight)
    # — should be zero on a clean exit, but surface it if non-zero.
    terminal_ids = {
        t.id for t in (*merged, *shipped_open, *blocked, *failed, *no_changes, *cancelled)
    }
    other = [t for t in tasks if t.id not in terminal_ids]

    say("")
    say("LEGION RUN COMPLETE")
    say(f"  Merged:     {len(merged)}")
    say(f"  Shipped:    {len(shipped_open)} (PR open, not yet merged)")
    say(f"  Blocked:    {len(blocked)}")
    say(f"  Failed:     {len(failed)}")
    say(f"  No changes: {len(no_changes)}")
    say(f"  Cancelled:  {len(cancelled)}")
    if other:
        say(f"  Other:      {len(other)} (non-terminal)")
    total = (
        len(merged) + len(shipped_open) + len(blocked)
        + len(failed) + len(no_changes) + len(cancelled) + len(other)
    )
    say(f"  Total:      {total}")

    for t in merged:
        if t.pr_url:
            say(f"    \u2713 {t.id}: {t.pr_url}")
    for t in blocked:
        reason = t.merge_blocker or "blocked"
        say(f"    \u26a0 {t.id}: {reason[:120]}")
    for t in failed:
        reason = t.error or t.status or "unknown"
        say(f"    \u2717 {t.id}: {reason[:120]}")
    for t in cancelled:
        say(f"    \u29d7 {t.id}: cancelled")

    # Non-zero exit when any task ended in a non-success terminal state
    # that indicates human attention is needed.
    if failed or blocked or cancelled:
        return 2
    return 0


def cmd_run(args) -> int:
    """Autonomous loop: dispatch ready tasks, poll in-flight, reconcile shipped.

    Exits when:
      - No in-flight + no ready + no shipped-but-unmerged tasks remain, OR
      - `.legion/STOP` is written (by /legion-stop or Ctrl-C handler).

    Resilient to individual spawn/poll/reconcile failures — logs the error
    and continues the next tick.
    """
    import signal
    tick_s = max(1, args.tick_seconds)
    quiet = args.quiet
    max_ticks = args.max_ticks or 0  # 0 = unbounded

    def _say(msg: str):
        if not quiet:
            print(msg, flush=True)

    # Handle Ctrl-C by writing STOP and letting the loop drain naturally.
    _original_handler = signal.getsignal(signal.SIGINT)
    def _on_sigint(_signum, _frame):
        _say("\n[run] SIGINT — writing STOP; in-flight workers will finish")
        state.write_stop("graceful")
        signal.signal(signal.SIGINT, _original_handler)
    signal.signal(signal.SIGINT, _on_sigint)

    tick = 0
    last_status: str = ""

    while True:
        tick += 1
        if max_ticks and tick > max_ticks:
            _say(f"[run] hit max_ticks={max_ticks}, exiting")
            break

        stop_mode = state.stop_requested()

        try:
            s = state.read_state()
        except Exception as e:
            print(f"[run] read_state failed: {e}", file=sys.stderr)
            return 1

        cfg = load_config()
        in_flight = state.in_flight_tasks(s)
        ready = state.ready_tasks(s)
        unmerged_shipped = [
            t for t in s.tasks.values()
            if t.status == "shipped" and t.merged_at is None and t.merge_blocker is None
        ]

        # ---- Exit condition ----
        if not in_flight and not ready and not unmerged_shipped:
            _say(f"[run] done at tick {tick}")
            break

        if stop_mode == "graceful" and not in_flight and not unmerged_shipped:
            _say(f"[run] graceful stop reached (no in-flight)")
            break

        if stop_mode == "force":
            _say(f"[run] force stop — exiting immediately")
            break

        # ---- Reap stale workers ----
        stale_ids = governor.stale_in_flight(s, cfg)
        for stale_id in stale_ids:
            stale_task = s.tasks[stale_id]
            _say(f"[run] reaping stale worker {stale_id} (in-flight > {cfg.budget.worker_timeout_minutes}m)")
            try:
                dispatch.kill(stale_task, force=True)
            except Exception as e:
                _say(f"[run]   kill failed: {e}")
            def _stale_mut(run: state.RunState, tid=stale_id):
                t = run.tasks[tid]
                t.status = "failed"
                t.finished_at = time.time()
                t.error = f"timed out after {cfg.budget.worker_timeout_minutes} minutes"
                run.events.append({
                    "ts": time.time(), "kind": "stale_reaped", "task_id": tid,
                })
            state.update_state(_stale_mut)

        # ---- Poll in-flight ----
        if in_flight:
            try:
                cmd_poll(argparse.Namespace()) if False else _silent_poll(s)
            except Exception as e:
                print(f"[run] poll error: {e}", file=sys.stderr)

        # ---- Reconcile shipped ----
        if unmerged_shipped:
            try:
                _silent_reconcile()
            except Exception as e:
                print(f"[run] reconcile error: {e}", file=sys.stderr)

        # ---- Spawn up to cap ----
        if not stop_mode:
            s = state.read_state()
            cap = governor.current_max_workers(s, cfg)
            slots = max(0, cap - len(state.in_flight_tasks(s)))
            ready = state.ready_tasks(s)
            spawned_this_tick = 0
            for task in ready[:slots]:
                try:
                    meta = dispatch.spawn(
                        task,
                        target=routing.route(task, cfg.dispatch, governor.throttle_active(s)).target,
                        repo_url=s.repo_url,
                        base_branch=s.base_branch,
                        branch_prefix=cfg.reconciler.branch_prefix,
                        auth_mode=cfg.swarm.auth_mode,
                    )
                except Exception as e:
                    print(f"[run] spawn {task.id} crashed: {e}", file=sys.stderr)
                    continue

                if not meta.get("worker_id"):
                    _say(f"[run] spawn {task.id} failed: {meta.get('spawn_error', 'unknown')[:120]}")
                    continue

                def _mut(run: state.RunState, tid=task.id, m=meta):
                    t = run.tasks[tid]
                    t.status = "in_flight"
                    t.target = m.get("target") or t.target
                    t.worker_id = m["worker_id"]
                    t.branch = m.get("branch")
                    t.dispatched_at = m.get("dispatched_at", time.time())
                    run.events.append({
                        "ts": time.time(), "kind": "spawn",
                        "task_id": tid, "target": t.target,
                    })
                state.update_state(_mut)
                spawned_this_tick += 1
                _say(f"[run] spawned {task.id} → {meta.get('target')} ({meta['worker_id']})")

        # ---- Status line on change ----
        s2 = state.read_state()
        shipped = sum(1 for t in s2.tasks.values() if t.status == "shipped")
        merged = sum(1 for t in s2.tasks.values() if t.merged_at is not None)
        inf = len(state.in_flight_tasks(s2))
        rdy = len(state.ready_tasks(s2))
        status = f"[tick {tick}] in_flight={inf} ready={rdy} shipped={shipped} merged={merged}"
        if status != last_status:
            _say(status)
            last_status = status

        time.sleep(tick_s)

    # Final summary
    s = state.read_state()
    exit_code = render_run_summary(s, _say)

    # Clear stop marker on clean exit
    state.clear_stop()
    return exit_code


def _silent_poll(_s):
    """Internal: run poll logic without printing JSON (loop manages its own output)."""
    # Re-use cmd_poll's logic but suppress stdout
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        cmd_poll(argparse.Namespace())


def _silent_reconcile():
    """Internal: run reconcile logic without printing JSON."""
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        cmd_reconcile(argparse.Namespace())


# ----------------------------------------------------------------------
# cleanup (idempotent reset)
# ----------------------------------------------------------------------

def cmd_cleanup(args) -> int:
    """Remove worktrees, delete legion/* branches, and (optionally) .legion/.

    Safe to run any time. Does NOT touch shipped PRs or the base branch.
    """
    removed_worktrees = []
    deleted_branches = []

    # 1. Find legion worktrees and remove them
    wt_list = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        capture_output=True, text=True,
    )
    for block in wt_list.stdout.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("worktree ") and "/.legion/worktrees/" in line:
                path = line.split(" ", 1)[1]
                r = subprocess.run(
                    ["git", "worktree", "remove", "--force", path],
                    capture_output=True, text=True,
                )
                if r.returncode == 0:
                    removed_worktrees.append(path)
    subprocess.run(["git", "worktree", "prune"], check=False, capture_output=True)

    # 2. Find and delete any legion/* branches
    branches = subprocess.run(
        ["git", "branch", "--list", "legion/*"],
        capture_output=True, text=True,
    )
    for line in branches.stdout.splitlines():
        b = line.strip().lstrip("* ").strip()
        if not b:
            continue
        r = subprocess.run(
            ["git", "branch", "-D", b],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            deleted_branches.append(b)

    # 3. Optionally wipe .legion/
    wiped_legion_dir = False
    if args.all:
        import shutil
        legion_dir = Path(".legion")
        if legion_dir.exists():
            shutil.rmtree(legion_dir)
            wiped_legion_dir = True

    print(json.dumps({
        "removed_worktrees": removed_worktrees,
        "deleted_branches": deleted_branches,
        "wiped_legion_dir": wiped_legion_dir,
    }, indent=2))
    return 0


# ----------------------------------------------------------------------
# Entry
# ----------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="legion")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init")
    p_init.add_argument("tasks", help="Path to tasks.json")
    p_init.add_argument("--repo-url", default=None)
    p_init.add_argument("--base-branch", default=None)
    p_init.set_defaults(func=cmd_init)

    p_ready = sub.add_parser("ready")
    p_ready.set_defaults(func=cmd_ready)

    p_route = sub.add_parser("route")
    p_route.add_argument("task_id")
    p_route.set_defaults(func=cmd_route)

    p_spawn = sub.add_parser("spawn")
    p_spawn.add_argument("task_id")
    p_spawn.add_argument("--target", choices=["local", "cloud"], default=None)
    p_spawn.set_defaults(func=cmd_spawn)

    p_poll = sub.add_parser("poll")
    p_poll.set_defaults(func=cmd_poll)

    p_status = sub.add_parser("status")
    p_status.set_defaults(func=cmd_status)

    p_scale = sub.add_parser("scale")
    p_scale.add_argument("n", help="Integer, or 'auto' to clear override")
    p_scale.set_defaults(func=cmd_scale)

    p_run = sub.add_parser("run", help="Autonomous dispatch + reconcile loop")
    p_run.add_argument("--tick-seconds", type=int, default=10,
                       help="Seconds between ticks (default 10)")
    p_run.add_argument("--max-ticks", type=int, default=0,
                       help="Max ticks before exit (default 0 = unlimited)")
    p_run.add_argument("--quiet", action="store_true")
    p_run.set_defaults(func=cmd_run)

    p_crit = sub.add_parser("critique",
                            help="Run deterministic+LLM critique on a tasks.json (no changes)")
    p_crit.add_argument("tasks", help="Path to tasks.json")
    p_crit.add_argument("--goal", default=None)
    p_crit.set_defaults(func=cmd_critique)

    p_ref = sub.add_parser("decompose-refine",
                           help="Iterate critique + refine up to --iterations times; overwrites the file")
    p_ref.add_argument("tasks", help="Path to tasks.json")
    p_ref.add_argument("--goal", default=None)
    p_ref.add_argument("--iterations", type=int, default=3)
    p_ref.add_argument("--dry-run", action="store_true",
                       help="Don't overwrite; just print the result")
    p_ref.set_defaults(func=cmd_decompose_refine)

    p_cost = sub.add_parser("cost")
    p_cost.set_defaults(func=cmd_cost)

    p_stop = sub.add_parser("stop")
    p_stop.add_argument("--force", action="store_true")
    p_stop.add_argument("--graceful", action="store_true")
    p_stop.set_defaults(func=cmd_stop)

    p_cap = sub.add_parser("cap")
    p_cap.set_defaults(func=cmd_cap)

    p_clean = sub.add_parser("cleanup", help="Remove legion worktrees + branches")
    p_clean.add_argument("--all", action="store_true",
                         help="Also rm -rf .legion/ (wipes run state)")
    p_clean.set_defaults(func=cmd_cleanup)

    p_rec = sub.add_parser("reconcile", help="Run one reconciliation pass (merge ready PRs, mediate conflicts)")
    p_rec.set_defaults(func=cmd_reconcile)

    p_med = sub.add_parser("mediate", help="Manually invoke the mediator on a task")
    p_med.add_argument("task_id")
    p_med.set_defaults(func=cmd_mediate)

    p_rev = sub.add_parser("review", help="Manually invoke the reviewer on a task")
    p_rev.add_argument("task_id")
    p_rev.set_defaults(func=cmd_review)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
