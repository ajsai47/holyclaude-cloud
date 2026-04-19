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

from . import dispatch, governor, mediator, reconciler, routing, state
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
    n = args.n
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
            def _mut(run: state.RunState, tid=task.id):
                run.tasks[tid].merge_blocker = "ci_failed"
                run.events.append({
                    "ts": time.time(), "kind": "merge_blocked",
                    "task_id": tid, "reason": "ci_failed",
                })
            state.update_state(_mut)
            results.append({"task_id": task.id, "status": "ci_failed"})
            continue

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
    p_scale.add_argument("n", type=int)
    p_scale.set_defaults(func=cmd_scale)

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

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
