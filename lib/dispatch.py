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

CLOUD_LOG_ROOT = Path(".legion/cloud_logs")


def spawn_cloud(task: Task, repo_url: str, base_branch: str, branch_prefix: str) -> dict:
    """Spawn a Modal worker as a background `modal run` subprocess.

    `modal run --detach` actually blocks until the remote function returns —
    "detach" only means the app persists beyond CLI exit, it doesn't mean
    async dispatch. So we Popen it in the background (like local workers)
    and track the CLI pid. Poll waits on pid exit and then pulls the
    worker's result.json from the Modal volume.

    Result of the function itself lands in the Modal Volume
    `holyclaude-cloud-worker-cache` at <task-id>/result.json, written by
    the worker before return. `poll_cloud` pulls it once the CLI exits.
    """
    try:
        modal_bin = find_modal_bin()
    except RuntimeError as e:
        return {
            "target": "cloud",
            "worker_id": None,
            "spawn_error": str(e),
            "dispatched_at": time.time(),
        }

    module_path = Path(__file__).parent.parent / "modal" / "worker.py"
    CLOUD_LOG_ROOT.mkdir(parents=True, exist_ok=True)
    log_path = CLOUD_LOG_ROOT / f"{task.id}.log"
    log_fh = open(log_path, "wb")

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
    proc = subprocess.Popen(
        cmd,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # so /legion-stop --force can killpg
    )
    return {
        "target": "cloud",
        "worker_id": f"pid:{proc.pid}",
        "branch": f"{branch_prefix}{task.id}",
        "log": str(log_path),
        "dispatched_at": time.time(),
    }


def poll_cloud(task: Task) -> dict | None:
    """Check if the background `modal run` subprocess has exited.
    None while still running; on exit, pull result.json from the volume."""
    if not task.worker_id or not task.worker_id.startswith("pid:"):
        return None
    pid = int(task.worker_id.split(":", 1)[1])
    try:
        os.kill(pid, 0)
        return None  # still running
    except ProcessLookupError:
        pass  # done

    # Subprocess exited. Pull result.json from the worker-cache Volume.
    result_path = Path(f".legion/cloud_results/{task.id}.json")
    result_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        modal_bin = find_modal_bin()
    except RuntimeError as e:
        return {"status": "failed", "error": f"modal CLI gone: {e}"}

    pull = subprocess.run(
        [modal_bin, "volume", "get",
         "holyclaude-cloud-worker-cache",
         f"{task.id}/result.json",
         str(result_path), "--force"],
        capture_output=True, text=True,
    )
    if pull.returncode != 0 or not result_path.exists():
        # Worker crashed before emitting result.json; surface the last
        # ~400 chars of the CLI log so we have some signal.
        log_path = CLOUD_LOG_ROOT / f"{task.id}.log"
        tail = ""
        if log_path.exists():
            try:
                tail = log_path.read_text(errors="replace")[-400:]
            except Exception:
                tail = "<unreadable log>"
        return {
            "status": "failed",
            "error": f"no result.json on volume; cli log tail: {tail}",
        }

    try:
        return json.loads(result_path.read_text())
    except Exception as e:
        return {"status": "failed", "error": f"bad result json: {e}"}


def kill_cloud(task: Task, signal_num: int = 15) -> bool:
    """Kill the `modal run` subprocess. Modal's detach mode means the
    cloud function keeps running on Modal's side — use
    `modal app stop <app-id>` for that."""
    if not task.worker_id or not task.worker_id.startswith("pid:"):
        return False
    pid = int(task.worker_id.split(":", 1)[1])
    try:
        os.killpg(pid, signal_num)
        return True
    except (ProcessLookupError, PermissionError):
        return False


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
    sig = 9 if force else 15
    if task.target == "local":
        return kill_local(task, signal_num=sig)
    if task.target == "cloud":
        return kill_cloud(task, signal_num=sig)
    return False
