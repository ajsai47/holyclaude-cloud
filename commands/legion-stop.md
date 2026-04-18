---
name: legion-stop
description: Stop the legion. --graceful lets in-flight workers finish; --force kills them mid-run.
argument-hint: "[--graceful|--force]"
---

# /legion-stop

Halt the swarm.

---

## Parse arguments

$ARGUMENTS — `--graceful` (default) or `--force`.

---

## Procedure

1. Read `./.legion/state.json`. If missing, say "No active legion run."

2. **Graceful** (default):
   - Write `.legion/STOP` with the current timestamp.
   - The orchestrator's dispatch loop checks for this file each iteration and stops dispatching new tasks.
   - In-flight workers complete naturally and push their PRs.
   - Print: "Stop signal sent. N workers in flight will finish; M queued tasks cancelled."

3. **Force**:
   - For each in-flight cloud worker, find the Modal function call ID in `state.json`.
   - Run: `/Users/ajsai47/tinker-env/bin/modal app stop holyclaude-cloud-worker` to terminate the whole worker app's containers.
   - Local subagents: there's no clean kill — note them as "abandoned" in state.json.
   - Print: "Force-killed N cloud workers. M local subagents will exit on their own. PR state may be inconsistent — check before merging."

4. Either way, write the final state to `.legion/state.json` with `status: "stopped"`.

---

## Note

`--force` can leave half-pushed branches and partial PRs. Use `--graceful` unless something is genuinely broken.
