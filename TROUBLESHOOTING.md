# Troubleshooting

Common failure modes + how to diagnose them. Every entry came from a real session.

## Setup & prerequisites

### "modal CLI not found"
Install: `pip install modal` into a Python env you control. The setup script checks common virtualenv locations then falls back to PATH. If your modal lives elsewhere, edit `MODAL_BIN_CANDIDATES` in `lib/dispatch.py`.

### "Could not locate Claude credentials"
On macOS: `security find-generic-password -s 'Claude Code-credentials' -w` should return JSON. If it errors, log into Claude Code once — that populates the keychain entry.
On Linux: expected at `~/.claude/.credentials.json`. If you've never run Claude Code locally, log in there.

### "legion-github secret missing GITHUB_TOKEN"
Run `gh auth login` then re-run `./setup`. The setup script reads `gh auth token` to populate the secret.

### Modal image build takes 15 min
First run only. Layers cached after that. If you hit this unexpectedly on a second run, someone bumped `HOLYCLAUDE_REF` or modified the apt/pip layers in `modal/worker.py`.

## Runtime

### Worker stuck "in-flight" forever
Most common cause: `claude-code` CLI hung after emitting its terminal `result` event. Our `poll_local` checks the stream-json log for `"type":"result"` and kills the zombie. If the log doesn't show a result marker either, the worker crashed before Claude finished — check `.legion/local_logs/<task-id>.log`.

Second cause: stale worker exceeded `worker_timeout_minutes`. `legion run` reaps these automatically; a single `legion poll` won't.

### "ramp stuck at cap=1"
`ramp_first_run = true` starts you at 1 worker and adds +1 per shipped task. If your first task fails or produces `no_changes`, cap doesn't grow. Options:
- `legion scale 3` to override
- Set `ramp_first_run = false` in `legion.toml`

### Throttle false-positive
Governor scans worker logs for 429 patterns. Pre-5b-calibration, it tripped on `"type":"rate_limit_event","status":"allowed"` (a normal event). Post-fix regex requires `status != "allowed"` or `429` in structured context. If you see spurious throttle halving, check `lib/governor.py` `THROTTLE_PATTERNS`.

### Worker pushed but PR didn't open
`gh pr create` failed. Usually auth (`GH_TOKEN`/`GITHUB_TOKEN` missing in the subprocess env), or a pre-existing branch with different history. Inspect the log at `.legion/local_logs/<task-id>.log`.

Re-running with the same `task-id` will force-push the branch (via `--force-with-lease`) and retry PR creation.

## Cloud workers specifically

### Cloud workers fail with `claude_failed` (401 / authentication expired)

**Symptom:** A cloud task shows `✗ T-001: claude_failed` with an error like _"Authentication failed (401). Your cloud session token has expired."_ The cloud log at `.legion/cloud_logs/<task-id>.log` contains `"error":"authentication_failed"` or `"api_error_status":401`.

**Cause:** Claude Pro session tokens are short-lived (~hours). The token that `setup` pushed to the Modal `claude-pro-session` secret has expired.

**Fix:**
```bash
cd ~/holyclaude-cloud
./setup          # re-reads your local ~/.claude/.credentials.json and pushes to Modal
```

Then retry the failed task:
```bash
legion spawn T-001   # or let `legion run --resume` pick it up
```

**Why this happens:** `setup` snapshots your current Claude Code session credentials into a Modal secret. If you log out of Claude Code locally, or if the session token rotates, the snapshot goes stale. Re-running `./setup` refreshes the snapshot.

If you're using `auth_mode = "api"` in `legion.toml`, this error means `ANTHROPIC_API_KEY` is unset or was pushed as a placeholder — export the real key and re-run `./setup`.

### `ModuleNotFoundError: No module named 'image'`
Historic bug — pre-fix, image definition was in a sibling `image.py` that Modal didn't ship with the worker code. Post-fix: image is inlined into `modal/worker.py`. If you see this, you're running very old code; `git pull`.

### `gh auth login --with-token` returns exit 1
Historic bug — pre-fix, the worker tried to use `gh auth login` which needs a writable config file. Post-fix: worker exports `GH_TOKEN` directly and embeds the token in the clone URL. If you see this, `git pull`.

### `--dangerously-skip-permissions cannot be used with root`
Historic bug — pre-fix, the container ran claude-code as root without setting `IS_SANDBOX=1`. Post-fix: worker sets `IS_SANDBOX=1` in the claude subprocess env. If you see this, `git pull`.

### Spawn completes instantly, marked "failed", but PR was actually opened
Historic bug — pre-fix, spawn expected `modal run --detach` to return an async `fc-XXX` ID. It actually blocks until the function finishes. Post-fix: spawn Popens modal as a background subprocess and polls pid. If you see this, `git pull`.

### Cloud worker shipped but `legion poll` shows no result
The worker writes `result.json` to the `holyclaude-cloud-worker-cache` Modal volume. `legion poll` pulls it via `modal volume get`. If the pull fails (network, perms), poll falls back to "unknown". Manually:
```
modal volume get holyclaude-cloud-worker-cache <task-id>/result.json - | cat
```

### Container runs out of disk during clone
Big repo. Either use `--target local`, or bump the worker's Modal resource allocation in `modal/worker.py` (`@app.function(memory=...)`).

## Reviewer

### Reviewer blocking normal, clean code
Check `.legion/review_logs/<task-id>.log`. If the reviewer's framing prompt is mis-reading the task spec, tighten the spec to be more explicit about intent.

### Reviewer missing real issues
Expected limit — 3 test cases of calibration. If you find false negatives, report them and we'll add to the reviewer prompt.

### "Diff truncated" warning on small diffs
Shouldn't happen — threshold is 40KB. If it does, check you're pointing `gh pr diff` at the right PR.

### Reviewer timeout
300s hardcoded. First attempt retries once automatically. If both fail, the reconciler falls through and merges without review (we prefer merge-without-review to block-on-reviewer-flakiness). Check `.legion/review_logs/<task-id>.log`.

## Reconciler / merges

### "Failed to delete local branch"
Benign. `gh pr merge --delete-branch` tries to delete the branch locally but the legion worktree has it checked out. Our `merge_pr` calls `gh pr view` to verify the actual state; if merged on GitHub, we return success. If you see this as a blocker, `git pull`.

### Mediator couldn't resolve
Exceeded `mediator_max_retries` (default 2). Check `.legion/mediator_logs/<task-id>.log` — Claude's reasoning is there. Manual resolution:
```bash
gh pr checkout <pr-number>
git fetch origin main
git merge origin/main
# resolve conflicts manually
git push
legion reconcile  # retry
```

### Task stuck in `ci_pending`
Either CI is genuinely running, or `gh pr checks` is returning stale data. If CI passed on GitHub but legion doesn't see it, force a retry:
```
legion reconcile
```
If still stuck, check `gh pr checks <pr-number> --json state,conclusion` output directly.

### `mergeable: UNKNOWN`
GitHub recomputes mergeability after every push. `wait_for_mergeable` polls for up to 45s. If it times out, the next `legion run` tick will retry automatically.

## Git / state

### `.legion/state.json` exists from a previous run
Either resume with `/legion-start --resume`, or wipe: `legion cleanup --all` (removes worktrees, legion/* branches, and `.legion/`).

### Dangling worktrees after crash
`git worktree list` shows entries marked "prunable". `legion cleanup` handles these:
```
legion cleanup          # removes worktrees + branches, keeps .legion/
legion cleanup --all    # also wipes .legion/
```

### Lock contention
`.legion/state.lock` is an flock. Concurrent `legion` CLI invocations serialize through it. If one hangs, kill the pid holding it:
```bash
lsof .legion/state.lock
```

## When all else fails

1. Re-read `gotchas.md` — every known sharp edge is there.
2. Check the Modal dashboard at `https://modal.com/apps/<your-workspace>/main/ap-<id>` for per-container logs.
3. Nuclear option: `legion cleanup --all && rm legion.toml && cp ~/holyclaude-cloud/config/legion.toml.example legion.toml` → fresh start.
