"""Mediator — resolve a merge conflict by spawning a Claude worker.

Called by the reconciler when `gh pr merge` fails with conflict. The
mediator:
  1. Creates a worktree at `.legion/mediators/<task-id>/` off base branch.
  2. Fetches the task's branch from origin.
  3. `git merge --no-commit origin/<task.branch>` — guaranteed to conflict
     (if not, we report no_conflict and the caller retries the merge).
  4. Spawns a Claude worker in that worktree with a mediator-framed prompt:
     "You're resolving this conflict. Preserve both intents."
  5. When Claude exits: checks for remaining conflict markers, commits,
     force-pushes to task.branch (replaces the loser's branch with the
     rebased+resolved version).
  6. Returns status dict.

The mediator is invoked synchronously from the reconciler. For Phase 3
it only runs local — running the mediator on cloud is a Phase 4 polish.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from shutil import which

from .state import Task


MEDIATOR_WT_ROOT = Path(".legion/mediators")
MEDIATOR_LOG_ROOT = Path(".legion/mediator_logs")
MEDIATOR_BRANCH_PREFIX = "legion-mediate/"


def _run(argv, cwd=None):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True)


def prepare_conflict_worktree(task: Task, base_branch: str) -> tuple[Path | None, str]:
    """Create a worktree on base_branch, merge task's branch. Returns
    (worktree_path, status). Status is 'conflict' if mediator is needed,
    'clean' if the merge went through fine (no mediator needed), or
    'prep_failed' with details."""
    if not task.branch:
        return None, "no_task_branch"

    wt = MEDIATOR_WT_ROOT / task.id
    wt.parent.mkdir(parents=True, exist_ok=True)

    # Nuke any stale mediator worktree for this task
    _run(["git", "worktree", "remove", "--force", str(wt)])
    _run(["git", "worktree", "prune"])

    med_branch = f"{MEDIATOR_BRANCH_PREFIX}{task.id}"
    _run(["git", "branch", "-D", med_branch])  # clean up stale mediator branch

    # Fresh worktree on base_branch (pulled from origin)
    _run(["git", "fetch", "origin", base_branch])
    create = _run(
        ["git", "worktree", "add", "-B", med_branch, str(wt), f"origin/{base_branch}"]
    )
    if create.returncode != 0:
        return None, f"worktree_create_failed: {create.stderr.strip()}"

    # Fetch + attempt merge
    _run(["git", "fetch", "origin", task.branch], cwd=wt)
    merge = _run(
        ["git", "merge", "--no-commit", "--no-ff", f"origin/{task.branch}"],
        cwd=wt,
    )
    if merge.returncode == 0:
        # Clean merge — caller should retry `gh pr merge` and mediator isn't needed
        return wt, "clean"

    return wt, "conflict"


def _framed_prompt(task: Task, wt: Path, base_branch: str) -> str:
    unmerged = _run(
        ["git", "diff", "--name-only", "--diff-filter=U"], cwd=wt
    ).stdout.strip().splitlines()
    file_list = "\n".join(f"  - {f}" for f in unmerged) or "  (none detected)"

    return (
        f"You are the MEDIATOR for task `{task.id}` in a HolyClaude legion.\n"
        f"\n"
        f"## Situation\n"
        f"Your task's branch `{task.branch}` has merge conflicts against `{base_branch}`.\n"
        f"A sibling task already merged into {base_branch} and touched the same code.\n"
        f"Both changes are valid. Your job is to resolve the conflict so BOTH intents survive.\n"
        f"\n"
        f"## Files with conflicts\n"
        f"{file_list}\n"
        f"\n"
        f"## Your original task spec (for context)\n"
        f"**{task.title}**\n"
        f"\n"
        f"{task.spec}\n"
        f"\n"
        f"## What to do\n"
        f"1. For each conflicted file, read it (you'll see <<<<<<<, =======, >>>>>>> markers).\n"
        f"   - The 'HEAD' side is what's currently on main (already shipped).\n"
        f"   - The 'origin/{task.branch}' side is your task's change.\n"
        f"2. Resolve each conflict so:\n"
        f"   - Main's change is preserved (it already shipped — don't revert it).\n"
        f"   - Your task's change is also applied on top of it.\n"
        f"   - If they genuinely cannot coexist, pick main's version and note why in a comment.\n"
        f"3. `git add` each resolved file.\n"
        f"4. `git commit -m 'mediate {task.id}: resolve conflicts with main'`\n"
        f"5. Stop. Do NOT push — the mediator harness will.\n"
        f"\n"
        f"## Boundaries\n"
        f"- Do NOT modify files that were not conflicted.\n"
        f"- Do NOT run tests, builds, or side effects.\n"
        f"- If the conflict is beyond your ability to resolve, write "
        f"`.legion/blockers/mediate-{task.id}.md` with your reasoning and stop.\n"
    )


def run_mediator(task: Task, base_branch: str) -> dict:
    """Main entry. Synchronous."""
    if not which("claude"):
        return {"status": "mediator_failed", "error": "`claude` CLI not on PATH"}
    if not which("gh"):
        return {"status": "mediator_failed", "error": "`gh` CLI not on PATH"}

    t_start = time.perf_counter()
    wt, prep_status = prepare_conflict_worktree(task, base_branch)

    if wt is None:
        return {"status": "mediator_failed", "error": prep_status}
    if prep_status == "clean":
        # Merge went fine — mediator not needed. Caller should retry `gh pr merge`.
        return {"status": "no_conflict", "note": "clean merge; retry merge_pr"}

    framed = _framed_prompt(task, wt, base_branch)
    MEDIATOR_LOG_ROOT.mkdir(parents=True, exist_ok=True)
    log_path = MEDIATOR_LOG_ROOT / f"{task.id}.log"

    with open(log_path, "wb") as log_fh:
        proc = subprocess.Popen(
            ["claude", "-p", framed,
             "--permission-mode", "bypassPermissions",
             "--output-format", "stream-json",
             "--verbose"],
            cwd=wt, stdout=log_fh, stderr=subprocess.STDOUT,
        )
        rc = proc.wait()

    if rc != 0:
        return {
            "status": "mediator_failed",
            "error": f"claude exited rc={rc}",
            "log": str(log_path),
        }

    # Any remaining conflicts?
    unresolved = _run(
        ["git", "diff", "--name-only", "--diff-filter=U"], cwd=wt
    ).stdout.strip()
    if unresolved:
        return {
            "status": "partial_resolve",
            "unresolved_files": unresolved.splitlines(),
            "log": str(log_path),
        }

    # Claude may have committed, or may have left uncommitted changes.
    porcelain = _run(["git", "status", "--porcelain"], cwd=wt).stdout.strip()
    if porcelain:
        # Commit whatever the mediator produced
        _run(["git", "add", "-A"], cwd=wt)
        commit = _run(
            ["git", "commit", "-m", f"mediate {task.id}: resolve conflicts with main"],
            cwd=wt,
        )
        if commit.returncode != 0 and "nothing to commit" not in (commit.stdout or "").lower():
            return {
                "status": "mediator_failed",
                "error": f"commit failed: {commit.stderr.strip()}",
            }

    # Blocker check — did the mediator give up?
    blocker_path = Path(f".legion/blockers/mediate-{task.id}.md")
    if blocker_path.exists():
        return {
            "status": "mediator_gave_up",
            "blocker": str(blocker_path),
        }

    # Force-push to replace the task's branch
    push = _run(
        ["git", "push", "--force-with-lease", "origin",
         f"HEAD:{task.branch}"],
        cwd=wt,
    )
    if push.returncode != 0:
        return {
            "status": "push_failed",
            "error": push.stderr.strip(),
        }

    return {
        "status": "resolved",
        "elapsed_s": round(time.perf_counter() - t_start, 1),
    }
