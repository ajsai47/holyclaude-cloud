"""One Modal cloud worker = one task in the legion.

Lifecycle:
  1. Receive task spec (id, title, prompt, repo_url, base_branch).
  2. Mount shared-brain volume so claude-mem state is shared across workers.
  3. Write Claude Pro session creds into ~/.claude/.credentials.json.
  4. Configure git + gh from the legion-github secret.
  5. Clone the repo into /workspace/repo, branch off base_branch.
  6. Run `claude -p` headlessly with the task prompt; HolyClaude is loaded
     from /opt/holyclaude (baked into image) so the worker has the full
     memory + workflow + team layers available.
  7. If Claude made commits, push the branch + open a PR via gh.
  8. Return a status dict the orchestrator can read.

Launch (from local orchestrator):
    /Users/ajsai47/tinker-env/bin/modal run modal/worker.py::run_task \\
        --task-id "T-001" \\
        --title "Add foo to bar" \\
        --prompt "..." \\
        --repo-url "https://github.com/ajsai47/myrepo.git" \\
        --base-branch "main"
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import modal

from image import image, shared_brain, worker_cache, SECRETS


app = modal.App("holyclaude-cloud-worker")

# Mount the shared-brain volume at the path claude-mem expects.
SHARED_BRAIN_MOUNT = "/root/.claude-mem"

# Per-task scratch dir on the worker-cache volume (one subdir per task-id).
WORKER_CACHE_MOUNT = "/cache"

# Where the repo gets cloned inside the container.
WORKSPACE = "/workspace/repo"


@app.function(
    image=image,
    timeout=60 * 30,  # 30-min per-worker default; overridden by orchestrator
    cpu=2,
    memory=4096,
    secrets=SECRETS,
    volumes={
        SHARED_BRAIN_MOUNT: shared_brain,
        WORKER_CACHE_MOUNT: worker_cache,
    },
    # Pro session won't survive massive parallelism. Hard cap here as a
    # second line of defense behind the local Governor.
    max_containers=10,
)
def run_task(
    task_id: str,
    title: str,
    prompt: str,
    repo_url: str,
    base_branch: str = "main",
    pr_body: str = "",
    branch_prefix: str = "legion/",
) -> dict:
    """Execute one task. Idempotent on task_id — re-running with same id
    will reuse the cached worktree if present."""
    t_start = time.perf_counter()
    log = []

    def step(msg: str):
        line = f"[worker:{task_id}] {msg}"
        print(line, flush=True)
        log.append(line)

    # ---------------------------------------------------------------
    # 1. Auth setup — Pro session creds + GitHub
    # ---------------------------------------------------------------
    creds_json = os.environ.get("CLAUDE_CREDENTIALS_JSON")
    if not creds_json:
        raise RuntimeError(
            "claude-pro-session secret must expose CLAUDE_CREDENTIALS_JSON. "
            "Run holyclaude-cloud's setup script."
        )
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    creds_path = claude_dir / ".credentials.json"
    creds_path.write_text(creds_json)
    creds_path.chmod(0o600)
    step("wrote Pro session creds")

    gh_token = os.environ.get("GITHUB_TOKEN")
    if not gh_token:
        raise RuntimeError("legion-github secret must expose GITHUB_TOKEN")
    # Configure gh + git
    subprocess.run(
        ["gh", "auth", "login", "--with-token"],
        input=gh_token, text=True, check=True,
    )
    subprocess.run(["gh", "auth", "setup-git"], check=True)
    subprocess.run(["git", "config", "--global", "user.email", "legion@holyclaude.local"], check=True)
    subprocess.run(["git", "config", "--global", "user.name", "HolyClaude Legion"], check=True)
    step("configured gh + git")

    # ---------------------------------------------------------------
    # 2. Clone the target repo
    # ---------------------------------------------------------------
    Path("/workspace").mkdir(parents=True, exist_ok=True)
    if Path(WORKSPACE).exists():
        # Cached from a previous run of this task-id — just refresh.
        subprocess.run(["git", "fetch", "origin", base_branch], cwd=WORKSPACE, check=True)
        subprocess.run(["git", "checkout", base_branch], cwd=WORKSPACE, check=True)
        subprocess.run(["git", "reset", "--hard", f"origin/{base_branch}"], cwd=WORKSPACE, check=True)
        step(f"refreshed cached worktree at {WORKSPACE}")
    else:
        subprocess.run(["git", "clone", repo_url, WORKSPACE], check=True)
        subprocess.run(["git", "checkout", base_branch], cwd=WORKSPACE, check=True)
        step(f"cloned {repo_url} -> {WORKSPACE}")

    branch_name = f"{branch_prefix}{task_id}"
    subprocess.run(["git", "checkout", "-B", branch_name], cwd=WORKSPACE, check=True)
    step(f"on branch {branch_name}")

    # ---------------------------------------------------------------
    # 3. Install HolyClaude as a user plugin so claude -p picks it up
    # ---------------------------------------------------------------
    # HolyClaude lives at /opt/holyclaude (baked in image). Symlink its
    # plugin manifest into ~/.claude/plugins/ so Claude Code loads it.
    plugins_dir = claude_dir / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    holyclaude_link = plugins_dir / "holyclaude"
    if not holyclaude_link.exists():
        holyclaude_link.symlink_to("/opt/holyclaude")
    step("linked HolyClaude into ~/.claude/plugins")

    # ---------------------------------------------------------------
    # 4. Run Claude headlessly on the task
    # ---------------------------------------------------------------
    # Frame the prompt with task context so the worker knows it's part of
    # a swarm and should make focused, atomic changes.
    framed_prompt = (
        f"You are worker {task_id} in a HolyClaude legion. Your task:\n\n"
        f"# {title}\n\n"
        f"{prompt}\n\n"
        f"Constraints:\n"
        f"- You're on branch `{branch_name}` off `{base_branch}`.\n"
        f"- Keep the change focused — only this task. The orchestrator dispatches sibling tasks separately.\n"
        f"- When you're done, stop. Don't ship/PR yourself — the worker harness will.\n"
        f"- If the task is unclear or impossible as specified, write your reasoning to .legion/blockers/{task_id}.md and stop.\n"
    )

    cmd = [
        "claude", "-p", framed_prompt,
        "--permission-mode", "bypassPermissions",
        "--output-format", "stream-json",
        "--verbose",
    ]
    step(f"running claude -p ({len(framed_prompt)} chars of prompt)")

    proc = subprocess.Popen(
        cmd,
        cwd=WORKSPACE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    transcript = []
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line); sys.stdout.flush()
        transcript.append(line)
    rc = proc.wait()
    step(f"claude exited rc={rc}")

    # Persist transcript to the cache volume for the orchestrator to read.
    cache_dir = Path(WORKER_CACHE_MOUNT) / task_id
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "transcript.jsonl").write_text("".join(transcript))
    (cache_dir / "log.txt").write_text("\n".join(log))

    if rc != 0:
        worker_cache.commit()
        return {
            "task_id": task_id,
            "status": "claude_failed",
            "returncode": rc,
            "elapsed_s": time.perf_counter() - t_start,
        }

    # ---------------------------------------------------------------
    # 5. Commit + push + open PR
    # ---------------------------------------------------------------
    # Did Claude actually change anything?
    diff_check = subprocess.run(
        ["git", "status", "--porcelain"], cwd=WORKSPACE,
        capture_output=True, text=True, check=True,
    )
    if not diff_check.stdout.strip():
        step("no changes — claude produced no diff")
        worker_cache.commit()
        return {
            "task_id": task_id,
            "status": "no_changes",
            "elapsed_s": time.perf_counter() - t_start,
        }

    subprocess.run(["git", "add", "-A"], cwd=WORKSPACE, check=True)
    subprocess.run(
        ["git", "commit", "-m", f"{task_id}: {title}"],
        cwd=WORKSPACE, check=True,
    )
    subprocess.run(
        ["git", "push", "-u", "origin", branch_name, "--force-with-lease"],
        cwd=WORKSPACE, check=True,
    )
    step(f"pushed branch {branch_name}")

    # Open PR. Body includes task body + a marker the reconciler reads.
    pr_full_body = (
        f"{pr_body or prompt}\n\n"
        f"---\n"
        f"<!-- legion-task-id: {task_id} -->\n"
        f"Spawned by HolyClaude Legion. Worker container: `{os.environ.get('MODAL_TASK_ID', 'unknown')}`.\n"
    )
    pr_create = subprocess.run(
        ["gh", "pr", "create",
         "--base", base_branch,
         "--head", branch_name,
         "--title", f"{task_id}: {title}",
         "--body", pr_full_body],
        cwd=WORKSPACE,
        capture_output=True, text=True,
    )
    pr_url = pr_create.stdout.strip() if pr_create.returncode == 0 else None
    if pr_url:
        step(f"opened PR: {pr_url}")
    else:
        step(f"gh pr create failed: {pr_create.stderr}")

    worker_cache.commit()
    shared_brain.commit()  # flush any claude-mem writes

    return {
        "task_id": task_id,
        "status": "shipped" if pr_url else "pushed_no_pr",
        "branch": branch_name,
        "pr_url": pr_url,
        "elapsed_s": time.perf_counter() - t_start,
    }


@app.local_entrypoint()
def main(
    task_id: str,
    title: str,
    prompt: str,
    repo_url: str,
    base_branch: str = "main",
):
    """Manual single-task launcher — used by /legion-start dispatcher.

    Real swarm dispatch goes through `run_task.spawn(...)` from the
    orchestrator, not this entrypoint.
    """
    result = run_task.remote(
        task_id=task_id,
        title=title,
        prompt=prompt,
        repo_url=repo_url,
        base_branch=base_branch,
    )
    print(json.dumps(result, indent=2))
