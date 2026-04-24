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


def auto_heal(state: RunState) -> list[dict]:
    """Check every shipped task against github and heal stale state.

    Covers:
      - task.merge_blocker set but PR is actually merged (clear blocker + set merged_at)
      - task.merged_at unset but PR is actually merged (set merged_at)

    Returns a list of {task_id, action} describing what was changed.
    Caller is responsible for persisting the returned mutations.
    """
    healed = []
    for t in state.tasks.values():
        if t.status != "shipped":
            continue
        if t.merged_at is not None and t.merge_blocker is None:
            continue  # already healthy
        num = pr_number(t.pr_url)
        if not num:
            continue
        if _pr_is_merged(num):
            healed.append({
                "task_id": t.id,
                "action": "healed_stale_blocker" if t.merge_blocker else "healed_missing_merged_at",
                "was_blocker": t.merge_blocker,
            })
    return healed


def pr_number(pr_url: str | None) -> str | None:
    if not pr_url:
        return None
    return pr_url.rstrip("/").split("/")[-1]


def _gh_repo(pr_url: str | None) -> list[str]:
    """Return ['--repo', 'owner/repo'] args for gh, extracted from the PR URL.
    Returns [] if the URL can't be parsed — gh will fall back to git-context
    inference (which may fail outside a repo, but at least we tried).
    """
    if not pr_url:
        return []
    parts = pr_url.rstrip("/").split("/")
    try:
        pull_idx = parts.index("pull")
        repo = f"{parts[pull_idx - 2]}/{parts[pull_idx - 1]}"
        return ["--repo", repo]
    except (ValueError, IndexError):
        return []


def check_ci(pr_url: str | None) -> str:
    """Returns one of: 'pass', 'fail', 'pending', 'none'.

    `gh pr checks` exits non-zero when there are no checks configured, or
    when they're failing. Use --json to disambiguate.
    """
    num = pr_number(pr_url)
    if not num:
        return "none"
    result = subprocess.run(
        ["gh", "pr", "checks", num, "--json", "state,bucket,name", *_gh_repo(pr_url)],
        capture_output=True, text=True,
    )
    # If no checks: gh exits 0 with `[]`, or exits 8 ("no checks")
    try:
        checks = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError:
        checks = []
    if not checks:
        return "none"
    # `bucket` values: "pass", "fail", "pending", "skipping"
    # `state` values: "SUCCESS", "FAILURE", "ERROR", "PENDING", "IN_PROGRESS", etc.
    has_fail = any(
        c.get("bucket") == "fail"
        or c.get("state", "").upper() in ("FAILURE", "ERROR", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED")
        for c in checks
    )
    if has_fail:
        return "fail"
    has_pending = any(
        c.get("bucket") == "pending"
        or c.get("state", "").upper() in ("PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED")
        for c in checks
    )
    if has_pending:
        return "pending"
    return "pass"


def _pr_is_merged(pr_num: str, pr_url: str | None = None) -> bool:
    """Query github for the actual merge state — independent of gh exit code."""
    result = subprocess.run(
        ["gh", "pr", "view", pr_num, "--json", "state", *_gh_repo(pr_url)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False
    try:
        return json.loads(result.stdout).get("state") == "MERGED"
    except Exception:
        return False


def fetch_ci_failure(pr_url: str | None) -> str:
    """Return a plain-text summary of the failing checks + their logs,
    for a CI re-dispatch to hand back to the next worker as context.
    """
    num = pr_number(pr_url)
    if not num:
        return "(no pr to fetch)"
    result = subprocess.run(
        ["gh", "pr", "checks", num, "--json", "name,state,bucket,link", *_gh_repo(pr_url)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return f"(gh pr checks failed: {result.stderr.strip()})"
    try:
        checks = json.loads(result.stdout)
    except Exception:
        return "(could not parse gh pr checks output)"
    failing = [
        c for c in checks
        if c.get("bucket") == "fail"
        or c.get("state", "").upper() in ("FAILURE", "ERROR", "TIMED_OUT", "ACTION_REQUIRED")
    ]
    if not failing:
        return "(no failing checks detected at fetch time)"

    lines = ["The following CI checks failed on the previous attempt's PR:"]
    for c in failing:
        name = c.get("name", "?")
        link = c.get("link", "")
        lines.append(f"- **{name}** — {link}")
    lines.append("")
    lines.append("The next worker should address these failures before shipping.")
    return "\n".join(lines)


def _get_merge_state(num: str, pr_url: str | None = None) -> str:
    """Synchronous query of GitHub's mergeStateStatus.
    Returns UNKNOWN on any failure."""
    result = subprocess.run(
        ["gh", "pr", "view", num, "--json", "mergeStateStatus", *_gh_repo(pr_url)],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        try:
            return json.loads(result.stdout).get("mergeStateStatus", "UNKNOWN")
        except Exception:
            pass
    return "UNKNOWN"


def merge_pr(task: Task, use_admin_merge: bool = False) -> dict:
    """Attempt to merge task's PR. Returns a result dict."""
    num = pr_number(task.pr_url)
    if not num:
        return {"status": "failed", "error": "no pr_url"}
    repo_args = _gh_repo(task.pr_url)

    # Stale-state recovery: if the PR is already merged (e.g. a prior
    # reconcile call failed to record merged_at), short-circuit.
    if _pr_is_merged(num, task.pr_url):
        return {"status": "merged", "note": "already merged on github"}

    result = subprocess.run(
        ["gh", "pr", "merge", num, "--squash", "--delete-branch",
         *repo_args, *(["--admin"] if use_admin_merge else [])],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return {"status": "merged"}

    err = ((result.stderr or "") + (result.stdout or "")).lower()

    # Benign case: GitHub side merged but gh failed to delete the local
    # branch (usually because a worktree still has it checked out). Verify.
    if "failed to delete" in err or "failed to delete local branch" in err:
        if _pr_is_merged(num, task.pr_url):
            return {"status": "merged", "note": "local branch delete skipped"}

    # Disambiguate failure modes via GitHub's mergeStateStatus. `gh pr merge`
    # prints "not mergeable" for BOTH real merge conflicts (DIRTY) and
    # branch-protection blocks (BLOCKED) — string-matching alone wrongly
    # invoked the mediator on branch-protected repos.
    merge_state = _get_merge_state(num, task.pr_url)
    if merge_state == "BLOCKED":
        return {
            "status": "needs_human",
            "error": result.stderr or result.stdout,
            "merge_state": merge_state,
        }
    if merge_state == "DIRTY":
        return {
            "status": "conflict",
            "error": result.stderr or result.stdout,
            "merge_state": merge_state,
        }
    if merge_state == "UNSTABLE":
        return {
            "status": "ci_blocked",
            "error": result.stderr or result.stdout,
            "merge_state": merge_state,
        }

    # Fallback: legacy pattern matching for states we couldn't read. Note
    # we intentionally dropped "not mergeable" from the conflict signature
    # here — that phrase collides with BLOCKED, and if we reach this fallback
    # we don't know which it is. Prefer the DIRTY/BLOCKED disambiguation above.
    if "merge conflict" in err or ("conflict" in err and "merge" in err):
        return {"status": "conflict", "error": result.stderr or result.stdout}
    if "check" in err and ("fail" in err or "pending" in err):
        return {"status": "ci_blocked", "error": result.stderr or result.stdout}

    # Final fallback: check actual state — if the merge went through despite
    # gh returning non-zero for some other reason, report merged.
    if _pr_is_merged(num, task.pr_url):
        return {"status": "merged", "note": "gh returned non-zero but PR is merged"}

    return {"status": "gh_error", "error": result.stderr or result.stdout}


def wait_for_mergeable(pr_url: str, timeout_s: int = 30, poll_s: int = 3) -> str:
    """Poll the PR's mergeStateStatus until it's actionable or timeout.
    Returns the last observed mergeStateStatus."""
    import time as _time
    num = pr_number(pr_url)
    if not num:
        return "no_pr"
    deadline = _time.time() + timeout_s
    last = "UNKNOWN"
    while _time.time() < deadline:
        result = subprocess.run(
            ["gh", "pr", "view", num, "--json", "mergeStateStatus,mergeable,state",
             *_gh_repo(pr_url)],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            try:
                d = json.loads(result.stdout)
                last = d.get("mergeStateStatus", "UNKNOWN")
                state = d.get("state")
                if state == "MERGED":
                    return "MERGED"
                # Actionable states — either merge now or known conflict
                if last in ("CLEAN", "HAS_HOOKS", "UNSTABLE", "BEHIND", "DIRTY", "BLOCKED"):
                    return last
            except Exception:
                pass
        _time.sleep(poll_s)
    return last
