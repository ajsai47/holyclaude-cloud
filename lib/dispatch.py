"""Uniform dispatch: spawn + poll for local subprocess AND Modal cloud workers.

Local worker:
  - git worktree at .legion/worktrees/<task-id>/
  - subprocess.Popen(["claude", "-p", framed_prompt, ...], cwd=worktree)
  - On exit: orchestrator does git commit + push + PR

Cloud worker:
  - modal.Function.lookup("holyclaude-cloud-worker", "run_task").spawn(...)
  - Returns immediately with FunctionCall ID
  - Poll via .get_call_graph() or re-hydrate FunctionCall.from_id()

Both produce the same status dict shape so the orchestrator's loop is uniform.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import LegionConfig
from .state import Task


MODAL_BIN_CANDIDATES = [
    "/Users/ajsai47/tinker-env/bin/modal",
    os.path.expanduser("~/tinker-env/bin/modal"),
]


def find_modal_bin() -> str:
    for cand in MODAL_BIN_CANDIDATES:
        if cand and os.access(cand, os.X_OK):
            return cand
    # Fall back to PATH
    from shutil import which
    found = which("modal")
    if found:
        return found
    raise RuntimeError("modal CLI not found. Run holyclaude-cloud/setup.")


WORKTREE_ROOT = Path(".legion/worktrees")
LOCAL_LOG_ROOT = Path(".legion/local_logs")


# ----------------------------------------------------------------------
# Framing — same prompt structure for local + cloud
# ----------------------------------------------------------------------

def frame_prompt(task: Task, base_branch: str, branch_name: str) -> str:
    return (
        f"You are worker {task.id} in a HolyClaude legion. Your task:\n\n"
        f"# {task.title}\n\n"
        f"{task.spec}\n\n"
        f"Constraints:\n"
        f"- You're on branch `{branch_name}` off `{base_branch}`.\n"
        f"- Keep the change focused — only this task. The orchestrator dispatches sibling tasks separately.\n"
        f"- When you're done, stop. Don't ship/PR yourself — the worker harness will.\n"
        f"- If the task is unclear or impossible as specified, write your reasoning to .legion/blockers/{task.id}.md and stop.\n"
    )


# ----------------------------------------------------------------------
# Local spawn (subprocess)
# ----------------------------------------------------------------------

def spawn_local(task: Task, base_branch: str, branch_prefix: str) -> dict:
    """Fire off a local `claude -p` in a git worktree. Returns spawn metadata."""
    from shutil import which
    if not which("claude"):
        return {
            "target": "local",
            "worker_id": None,
            "spawn_error": "`claude` CLI not found on PATH. Install: npm i -g @anthropic-ai/claude-code",
            "dispatched_at": time.time(),
        }
    if not which("gh"):
        return {
            "target": "local",
            "worker_id": None,
            "spawn_error": "`gh` CLI not found on PATH. Install: brew install gh (or apt install gh)",
            "dispatched_at": time.time(),
        }

    branch_name = f"{branch_prefix}{task.id}"
    worktree = WORKTREE_ROOT / task.id
    worktree.parent.mkdir(parents=True, exist_ok=True)

    # Prune dangling worktree registrations (e.g. after `rm -rf .legion`).
    subprocess.run(["git", "worktree", "prune"], check=False, capture_output=True)

    if not worktree.exists():
        create = subprocess.run(
            ["git", "worktree", "add", "-B", branch_name, str(worktree), base_branch],
            capture_output=True, text=True,
        )
        if create.returncode != 0:
            return {
                "target": "local",
                "worker_id": None,
                "spawn_error": f"git worktree add failed: {create.stderr.strip()}",
                "dispatched_at": time.time(),
            }
    else:
        # Re-run of same task-id; refresh.
        subprocess.run(["git", "fetch", "origin", base_branch], cwd=worktree, check=False)
        subprocess.run(["git", "checkout", "-B", branch_name, base_branch], cwd=worktree, check=False)

    LOCAL_LOG_ROOT.mkdir(parents=True, exist_ok=True)
    log_path = LOCAL_LOG_ROOT / f"{task.id}.log"
    log_fh = open(log_path, "wb")

    framed = frame_prompt(task, base_branch, branch_name)
    cmd = [
        "claude", "-p", framed,
        "--permission-mode", "bypassPermissions",
        "--output-format", "stream-json",
        "--verbose",
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=worktree,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # so /legion-stop can kill cleanly
    )
    return {
        "target": "local",
        "worker_id": f"pid:{proc.pid}",
        "branch": branch_name,
        "worktree": str(worktree),
        "log": str(log_path),
        "dispatched_at": time.time(),
    }


def poll_local(task: Task, base_branch: str) -> dict | None:
    """Check if the local subprocess has exited. None if still running.

    On exit: inspect worktree, handle commit/push/PR, return terminal status.
    All git/gh calls are `check=False` so a failure surfaces as a status
    dict rather than crashing the poll loop.
    """
    if not task.worker_id or not task.worker_id.startswith("pid:"):
        return None
    pid = int(task.worker_id.split(":", 1)[1])
    try:
        os.kill(pid, 0)
        return None  # still running
    except ProcessLookupError:
        pass  # done

    branch = task.branch or ""
    worktree = WORKTREE_ROOT / task.id
    if not worktree.exists():
        return {"status": "failed", "error": "worktree disappeared"}

    def _run(argv, **kw):
        return subprocess.run(argv, cwd=worktree, capture_output=True, text=True, **kw)

    # Blocker marker?
    blocker_path = Path(f".legion/blockers/{task.id}.md")
    if blocker_path.exists():
        return {"status": "failed", "error": f"blocker: see {blocker_path}"}

    # Claude made uncommitted changes?
    diff_check = _run(["git", "status", "--porcelain"])
    has_uncommitted = bool(diff_check.stdout.strip())

    # Claude made commits on its own?
    commits_ahead_proc = _run(
        ["git", "rev-list", "--count", f"origin/{base_branch}..HEAD"]
    )
    try:
        commits_ahead = int((commits_ahead_proc.stdout or "0").strip())
    except ValueError:
        commits_ahead = 0

    if not has_uncommitted and commits_ahead == 0:
        return {"status": "no_changes"}

    # Commit anything uncommitted
    if has_uncommitted:
        add = _run(["git", "add", "-A"])
        if add.returncode != 0:
            return {"status": "failed", "error": f"git add: {add.stderr}"}
        commit = _run(["git", "commit", "-m", f"{task.id}: {task.title}"])
        if commit.returncode != 0:
            return {"status": "failed", "error": f"git commit: {commit.stderr}"}

    # Push
    push = _run(["git", "push", "-u", "origin", branch, "--force-with-lease"])
    if push.returncode != 0:
        return {"status": "failed", "error": f"git push: {push.stderr}"}

    # Open PR (against the actual base branch)
    pr_body = (
        f"{task.spec}\n\n---\n<!-- legion-task-id: {task.id} -->\n"
        f"Spawned by HolyClaude Legion (local worker).\n"
    )
    pr_result = _run([
        "gh", "pr", "create",
        "--base", base_branch,
        "--head", branch,
        "--title", f"{task.id}: {task.title}",
        "--body", pr_body,
    ])
    if pr_result.returncode != 0:
        # Common case: PR already exists (re-run of same task-id). Fetch its URL.
        existing = _run(["gh", "pr", "view", branch, "--json", "url", "-q", ".url"])
        if existing.returncode == 0 and existing.stdout.strip():
            return {"status": "shipped", "pr_url": existing.stdout.strip()}
        return {"status": "pushed_no_pr", "error": f"gh pr create: {pr_result.stderr}"}

    return {"status": "shipped", "pr_url": pr_result.stdout.strip()}


def kill_local(task: Task, signal_num: int = 15) -> bool:
    if not task.worker_id or not task.worker_id.startswith("pid:"):
        return False
    pid = int(task.worker_id.split(":", 1)[1])
    try:
        os.killpg(pid, signal_num)
        return True
    except (ProcessLookupError, PermissionError):
        return False


# ----------------------------------------------------------------------
# Cloud spawn (Modal)
# ----------------------------------------------------------------------

def spawn_cloud(task: Task, repo_url: str, base_branch: str, branch_prefix: str) -> dict:
    """Spawn a Modal worker via `modal run --detach`, capturing the FunctionCall ID.

    We shell out to the modal CLI rather than using the Python SDK because
    the orchestrator runs wherever Claude Code lives — not necessarily in
    the same venv as modal.
    """
    modal_bin = find_modal_bin()
    # `modal run --detach` prints the FunctionCall ID on stdout.
    # We invoke run_task directly with CLI args.
    module_path = Path(__file__).parent.parent / "modal" / "worker.py"

    cmd = [
        modal_bin, "run", "--detach",
        f"{module_path}::run_task",
        "--task-id", task.id,
        "--title", task.title,
        "--prompt", task.spec,
        "--repo-url", repo_url,
        "--base-branch", base_branch,
        "--branch-prefix", branch_prefix,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return {
            "target": "cloud",
            "worker_id": None,
            "spawn_error": result.stderr,
            "dispatched_at": time.time(),
        }

    # Parse FunctionCall ID out of the modal output.
    # modal CLI emits something like "fc-Abc123XYZ...". Match liberally.
    import re as _re
    call_id = None
    for source in (result.stdout, result.stderr):
        match = _re.search(r"\bfc-[a-zA-Z0-9]+\b", source or "")
        if match:
            call_id = match.group(0)
            break

    return {
        "target": "cloud",
        "worker_id": f"modal:{call_id}" if call_id else None,
        "branch": f"{branch_prefix}{task.id}",
        "dispatched_at": time.time(),
        "spawn_stdout": result.stdout[-500:],
    }


def poll_cloud(task: Task) -> dict | None:
    """Check a Modal FunctionCall. Returns status dict or None if still running."""
    if not task.worker_id or not task.worker_id.startswith("modal:"):
        return None
    call_id = task.worker_id.split(":", 1)[1]
    if not call_id or call_id == "None":
        return {"status": "failed", "error": "no call id recorded"}

    modal_bin = find_modal_bin()
    # `modal call-logs <id>` prints logs; `modal call <id>` isn't a thing.
    # Cleanest: use the Python SDK via a tiny helper script.
    # Even cleaner: the worker writes its result to the worker-cache Volume.
    # Check for a result marker in .legion/cloud_results/<task-id>.json which
    # the poll subcommand pulls via `modal volume get` before calling us.
    result_path = Path(f".legion/cloud_results/{task.id}.json")
    if result_path.exists():
        try:
            return json.loads(result_path.read_text())
        except Exception as e:
            return {"status": "failed", "error": f"bad result json: {e}"}
    return None  # still running


def kill_cloud(task: Task) -> bool:
    """Cancel a Modal FunctionCall via CLI."""
    if not task.worker_id or not task.worker_id.startswith("modal:"):
        return False
    call_id = task.worker_id.split(":", 1)[1]
    modal_bin = find_modal_bin()
    # modal call cancel <fc-id>
    result = subprocess.run(
        [modal_bin, "call", "cancel", call_id],
        capture_output=True, text=True,
    )
    return result.returncode == 0


# ----------------------------------------------------------------------
# Uniform interface
# ----------------------------------------------------------------------

def spawn(task: Task, target: str, repo_url: str, base_branch: str, branch_prefix: str) -> dict:
    if target == "local":
        return spawn_local(task, base_branch, branch_prefix)
    if target == "cloud":
        return spawn_cloud(task, repo_url, base_branch, branch_prefix)
    raise ValueError(f"unknown target {target!r}")


def poll(task: Task, base_branch: str) -> dict | None:
    if task.target == "local":
        return poll_local(task, base_branch)
    if task.target == "cloud":
        return poll_cloud(task)
    return None


def kill(task: Task, force: bool = False) -> bool:
    if task.target == "local":
        return kill_local(task, signal_num=9 if force else 15)
    if task.target == "cloud":
        return kill_cloud(task)
    return False
