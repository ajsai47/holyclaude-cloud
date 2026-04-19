"""Reconciler — dependency-ordered PR merging.

Flow per task:
  1. All deps merged?        → no: skip (wait for deps)
  2. Task shipped + has PR?  → no: skip
  3. CI state?               → fail: mark merge_blocker=ci_failed
                             → pending: skip until next reconcile
                             → pass/none: proceed to merge
  4. `gh pr merge --squash`  → success: mark merged_at
                             → conflict: invoke mediator
                             → other fail: mark merge_blocker=gh_error

The reconciler is idempotent and designed to be called on a tick (every
few seconds by the orchestrator's main loop, or once by /legion-reconcile).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .state import RunState, Task


def ready_to_merge(state: RunState) -> list[Task]:
    """Shipped tasks whose deps are all merged and which don't have a blocker yet."""
    merged_ids = {tid for tid, t in state.tasks.items() if t.merged_at is not None}
    return [
        t for t in state.tasks.values()
        if t.status == "shipped"
        and t.merged_at is None
        and t.merge_blocker is None
        and all(d in merged_ids for d in t.deps)
    ]


def pr_number(pr_url: str | None) -> str | None:
    if not pr_url:
        return None
    return pr_url.rstrip("/").split("/")[-1]


def check_ci(pr_url: str | None) -> str:
    """Returns one of: 'pass', 'fail', 'pending', 'none'.

    `gh pr checks` exits non-zero when there are no checks configured, or
    when they're failing. Use --json to disambiguate.
    """
    num = pr_number(pr_url)
    if not num:
        return "none"
    result = subprocess.run(
        ["gh", "pr", "checks", num, "--json", "state,conclusion,name"],
        capture_output=True, text=True,
    )
    # If no checks: gh exits 0 with `[]`, or exits 8 ("no checks")
    try:
        checks = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError:
        checks = []
    if not checks:
        return "none"
    has_fail = any(c.get("conclusion") == "failure" for c in checks)
    if has_fail:
        return "fail"
    has_pending = any(
        c.get("state") in ("pending", "queued", "in_progress")
        or c.get("conclusion") is None
        for c in checks
    )
    if has_pending:
        return "pending"
    return "pass"


def merge_pr(task: Task) -> dict:
    """Attempt to merge task's PR. Returns a result dict."""
    num = pr_number(task.pr_url)
    if not num:
        return {"status": "failed", "error": "no pr_url"}
    result = subprocess.run(
        ["gh", "pr", "merge", num, "--squash", "--delete-branch"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return {"status": "merged"}
    err = ((result.stderr or "") + (result.stdout or "")).lower()
    if "conflict" in err or "not mergeable" in err or "merge conflict" in err:
        return {"status": "conflict", "error": result.stderr or result.stdout}
    if "check" in err and ("fail" in err or "pending" in err):
        return {"status": "ci_blocked", "error": result.stderr or result.stdout}
    return {"status": "gh_error", "error": result.stderr or result.stdout}
