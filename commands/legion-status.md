---
name: legion-status
description: Show the current state of an active HolyClaude legion run.
argument-hint: "[task-id to tail transcript]"
---

# /legion-status

What's the swarm doing right now.

---

## Procedure

Run `~/holyclaude-cloud/bin/legion status` and render the output.

If the user passed a task ID as argument, also tail that task's transcript:
- Local: `tail -n 50 .legion/local_logs/<task-id>.log`
- Cloud: `tail -n 50 .legion/cloud_results/<task-id>.json` (summary) — for full transcript run `/Users/ajsai47/tinker-env/bin/modal volume get holyclaude-cloud-worker-cache <task-id>/transcript.jsonl -`.

If `.legion/state.json` doesn't exist, tell the user: "No active legion run in this repo. Start one with `/legion-start`."
