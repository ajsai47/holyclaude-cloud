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

Run `~/holyclaude-cloud/bin/legion stop` with the appropriate flag.

- `--graceful` (default): writes `.legion/STOP`, orchestrator's dispatch loop drains on next poll. In-flight workers complete; queued tasks cancel.
- `--force`: kills all in-flight workers (local SIGKILL, cloud Modal FunctionCall cancel), marks them `failed` with error `force-stopped`, writes `STOP`. PR state may be inconsistent.

Show the user the CLI's output and add:
- PRs that shipped before the stop (from `legion status`)
- Half-pushed branches after `--force` to investigate manually

---

## Note

After a stop, the orchestrator's dispatch loop (if still running in another Claude session) will exit on its next iteration. If no dispatch loop is running (you already quit the session), `legion stop --graceful` just writes the marker — nothing will act on it until a new `/legion-start --resume` happens (Phase 4).

If the run is permanently dead and you want to clear state: `rm -rf .legion/`.
