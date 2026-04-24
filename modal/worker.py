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


# ======================================================================
# Image — inlined from image.py. Modal doesn't ship sibling .py files
# in the image by default; keeping the image definition and function in
# a single file avoids ModuleNotFoundError inside the container.
# See image.py for the documented, canonical definition.
# ======================================================================

HOLYCLAUDE_REPO = "https://github.com/ajsai47/holyclaude.git"
# Pinned to avoid silent upstream changes breaking cloud workers.
# Bump intentionally; see docs/UPGRADING_HOLYCLAUDE.md (TODO).
HOLYCLAUDE_REF = "b80d41f0cf39"
NODE_MAJOR = "20"
PLAYWRIGHT_VERSION = "1.48.0"

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install(
        "curl", "git", "ca-certificates", "gnupg", "build-essential",
        "unzip", "jq",
        "libnss3", "libatk1.0-0", "libatk-bridge2.0-0", "libcups2",
        "libdrm2", "libxkbcommon0", "libxcomposite1", "libxdamage1",
        "libxrandr2", "libgbm1", "libpango-1.0-0", "libcairo2", "libasound2",
    )
    .run_commands(
        f"curl -fsSL https://deb.nodesource.com/setup_{NODE_MAJOR}.x | bash -",
        "apt-get install -y nodejs",
    )
    .run_commands("npm install -g @anthropic-ai/claude-code")
    .run_commands(
        "curl -fsSL https://bun.sh/install | bash",
        "ln -s /root/.bun/bin/bun /usr/local/bin/bun",
    )
    .pip_install(
        "tomli==2.0.2",
        "httpx==0.27.2",
        "rich==13.9.4",
    )
    .run_commands(
        f"git clone {HOLYCLAUDE_REPO} /opt/holyclaude",
        f"cd /opt/holyclaude && git checkout {HOLYCLAUDE_REF}",
        "cd /opt/holyclaude && bun install --frozen-lockfile || bun install || true",
    )
    .run_commands(
        f"npm install -g playwright@{PLAYWRIGHT_VERSION}",
        "npx playwright install chromium --with-deps",
    )
    .run_commands(
        "curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | "
        "  dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg",
        "chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg",
        'echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] '
        'https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list',
        "apt-get update",
        "apt-get install -y gh",
    )
)

SHARED_BRAIN_VOLUME = "holyclaude-cloud-shared-brain"
WORKER_CACHE_VOLUME = "holyclaude-cloud-worker-cache"

shared_brain = modal.Volume.from_name(SHARED_BRAIN_VOLUME, create_if_missing=True)
worker_cache = modal.Volume.from_name(WORKER_CACHE_VOLUME, create_if_missing=True)

# Attach both auth secrets. `anthropic-api-key` may be a placeholder
# (setup pushes one if ANTHROPIC_API_KEY isn't in the user's env); the
# worker runtime checks if the value is a real key before using it.
# Both secrets MUST exist — run `~/holyclaude-cloud/setup` to push them.
SECRETS = [
    modal.Secret.from_name("claude-pro-session"),
    modal.Secret.from_name("anthropic-api-key"),
    modal.Secret.from_name("legion-github"),
]


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
    auth_mode: str = "session",
) -> dict:
    """Execute one task. Idempotent on task_id — re-running with same id
    will reuse the cached worktree if present."""
    t_start = time.perf_counter()
    log = []

    def step(msg: str):
        line = f"[worker:{task_id}] {msg}"
        print(line, flush=True)
        log.append(line)

    # Early-emit helper for catastrophic failures so poll_cloud doesn't hang.
    def _emit_crash(err: str) -> dict:
        crash_dir = Path(WORKER_CACHE_MOUNT) / task_id
        try:
            crash_dir.mkdir(parents=True, exist_ok=True)
            (crash_dir / "log.txt").write_text("\n".join(log))
            (crash_dir / "result.json").write_text(json.dumps({
                "task_id": task_id,
                "status": "failed",
                "error": err,
                "elapsed_s": time.perf_counter() - t_start,
            }, indent=2))
            worker_cache.commit()
        except Exception as commit_err:
            print(f"[worker:{task_id}] failed to emit crash result: {commit_err}")
        return {"task_id": task_id, "status": "failed", "error": err}

    try:
        return _run_task_body(task_id, title, prompt, repo_url, base_branch,
                              pr_body, branch_prefix, t_start, log, step,
                              auth_mode=auth_mode)
    except subprocess.CalledProcessError as e:
        err = f"subprocess failed: {' '.join(str(a) for a in e.cmd)} -> rc={e.returncode}"
        if e.stderr:
            err += f"  stderr={e.stderr[-400:]}"
        step(err)
        return _emit_crash(err)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        step(f"unexpected error: {e}\n{tb}")
        return _emit_crash(f"{type(e).__name__}: {e}\n{tb}")


def _run_task_body(
    task_id, title, prompt, repo_url, base_branch,
    pr_body, branch_prefix, t_start, log, step,
    auth_mode="session",
):
    # ---------------------------------------------------------------
    # 1. Auth setup — pick auth_mode
    # ---------------------------------------------------------------
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    if auth_mode == "api":
        api_key = os.environ.get("ANTHROPIC_API_KEY") or ""
        if not api_key or api_key.startswith("placeholder-"):
            raise RuntimeError(
                "auth_mode=api but ANTHROPIC_API_KEY is unset or a "
                "placeholder. Run `~/holyclaude-cloud/setup` with "
                "ANTHROPIC_API_KEY exported in your shell to push the real "
                "key, then retry."
            )
        # Clear any pro session creds to avoid claude-code preferring them
        creds_path = claude_dir / ".credentials.json"
        if creds_path.exists():
            creds_path.unlink()
        step("using ANTHROPIC_API_KEY for auth")
    else:
        creds_json = os.environ.get("CLAUDE_CREDENTIALS_JSON")
        if not creds_json:
            raise RuntimeError(
                "auth_mode=session but claude-pro-session secret is missing "
                "CLAUDE_CREDENTIALS_JSON. Run holyclaude-cloud's setup script, "
                "or switch legion.toml [swarm] auth_mode = 'api'."
            )
        creds_path = claude_dir / ".credentials.json"
        creds_path.write_text(creds_json)
        creds_path.chmod(0o600)
        # If ANTHROPIC_API_KEY is also set (from the api secret), unset it so
        # claude-code doesn't prefer it over the session creds
        os.environ.pop("ANTHROPIC_API_KEY", None)
        step("wrote Pro session creds")

    gh_token = os.environ.get("GITHUB_TOKEN")
    if not gh_token:
        raise RuntimeError("legion-github secret must expose GITHUB_TOKEN")

    # gh picks up GH_TOKEN/GITHUB_TOKEN automatically — no `gh auth login` needed
    # (which requires a config file that's trickier to bootstrap in a container).
    os.environ["GH_TOKEN"] = gh_token
    subprocess.run(["git", "config", "--global", "user.email", "legion@holyclaude.local"], check=True)
    subprocess.run(["git", "config", "--global", "user.name", "HolyClaude Legion"], check=True)
    step("configured gh + git (token in env)")

    # ---------------------------------------------------------------
    # 2. Clone the target repo — token embedded in URL so both clone
    #    and push work without a credential helper.
    # ---------------------------------------------------------------
    import re as _re
    authed_url = _re.sub(
        r"^https://(?:[^@/]+@)?",
        f"https://x-access-token:{gh_token}@",
        repo_url,
    )

    Path("/workspace").mkdir(parents=True, exist_ok=True)
    if Path(WORKSPACE).exists():
        # Cached from a previous run of this task-id — refresh.
        subprocess.run(["git", "remote", "set-url", "origin", authed_url], cwd=WORKSPACE, check=False)
        subprocess.run(["git", "fetch", "origin", base_branch], cwd=WORKSPACE, check=True)
        subprocess.run(["git", "checkout", base_branch], cwd=WORKSPACE, check=True)
        subprocess.run(["git", "reset", "--hard", f"origin/{base_branch}"], cwd=WORKSPACE, check=True)
        step(f"refreshed cached worktree at {WORKSPACE}")
    else:
        subprocess.run(["git", "clone", authed_url, WORKSPACE], check=True)
        subprocess.run(["git", "checkout", base_branch], cwd=WORKSPACE, check=True)
        step(f"cloned -> {WORKSPACE}")

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

    # Disable target-repo .mcp.json before running claude — if any MCP server
    # in the target repo's config fails to init (network, missing creds), Claude
    # Code's startup errors out immediately. Workers run in containers without
    # those credentials, so MCP init is CI's job, not the worker's.
    mcp_json = Path(WORKSPACE) / ".mcp.json"
    mcp_json_disabled = Path(WORKSPACE) / ".mcp.json.legion-disabled"
    if mcp_json.exists():
        mcp_json.rename(mcp_json_disabled)
        step("renamed .mcp.json -> .mcp.json.legion-disabled (MCP servers disabled in worker containers; CI will validate them)")

    cmd = [
        "claude", "-p", framed_prompt,
        "--permission-mode", "bypassPermissions",
        "--output-format", "stream-json",
        "--verbose",
    ]
    step(f"running claude -p ({len(framed_prompt)} chars of prompt)")

    # Modal containers run as root. Claude Code refuses to bypass permissions
    # when UID=0 unless IS_SANDBOX=1 is set to acknowledge the environment.
    claude_env = {**os.environ, "IS_SANDBOX": "1"}
    proc = subprocess.Popen(
        cmd,
        cwd=WORKSPACE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=claude_env,
    )
    transcript = []
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line); sys.stdout.flush()
        transcript.append(line)
    rc = proc.wait()
    step(f"claude exited rc={rc}")

    # Restore .mcp.json if we disabled it above.
    if mcp_json_disabled.exists():
        mcp_json_disabled.rename(mcp_json)
        step("restored .mcp.json")

    # Persist transcript to the cache volume for the orchestrator to read.
    cache_dir = Path(WORKER_CACHE_MOUNT) / task_id
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "transcript.jsonl").write_text("".join(transcript))
    (cache_dir / "log.txt").write_text("\n".join(log))

    def _emit_result(d: dict) -> dict:
        """Write result.json to the cache volume so poll_cloud can pull it."""
        (cache_dir / "result.json").write_text(json.dumps(d, indent=2))
        worker_cache.commit()
        return d

    if rc != 0:
        return _emit_result({
            "task_id": task_id,
            "status": "claude_failed",
            "returncode": rc,
            "elapsed_s": time.perf_counter() - t_start,
        })

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
        return _emit_result({
            "task_id": task_id,
            "status": "no_changes",
            "elapsed_s": time.perf_counter() - t_start,
        })

    subprocess.run(["git", "add", "-A"], cwd=WORKSPACE, check=True)

    # Skip pre-commit hooks if the target repo has them configured. Workers
    # run in containers that lack the hook dependencies (linters, formatters,
    # etc.). Hooks are CI's responsibility; --no-verify is safe here.
    commit_cmd = ["git", "commit", "-m", f"{task_id}: {title}"]
    if (Path(WORKSPACE) / ".pre-commit-config.yaml").exists():
        commit_cmd.append("--no-verify")
        step("target repo has .pre-commit-config.yaml — using --no-verify (hook deps not available in worker container; CI will enforce hooks on the PR)")
        pr_full_body_hook_note = "\n> **Note:** pre-commit hooks were skipped during the worker commit (`--no-verify`). CI will enforce them on this PR.\n"
    else:
        pr_full_body_hook_note = ""

    subprocess.run(commit_cmd, cwd=WORKSPACE, check=True)
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
        f"{pr_full_body_hook_note}"
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

    shared_brain.commit()  # flush any claude-mem writes

    return _emit_result({
        "task_id": task_id,
        "status": "shipped" if pr_url else "pushed_no_pr",
        "branch": branch_name,
        "pr_url": pr_url,
        "elapsed_s": time.perf_counter() - t_start,
    })


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
