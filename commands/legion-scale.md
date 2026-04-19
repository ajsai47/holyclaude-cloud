---
name: legion-scale
description: Override the legion's max_workers cap at runtime. Useful when you want to push past the ramp, or dial back after seeing throttle.
argument-hint: "<n>"
---

# /legion-scale

Set the in-flight worker cap for the current run.

---

## Parse arguments

$ARGUMENTS — a single integer N.

If no number was given, STOP and use AskUserQuestion: "How many workers? (1-10)"

---

## Procedure

1. Run `~/holyclaude-cloud/bin/legion scale <n>`.
2. Then run `~/holyclaude-cloud/bin/legion cap` and show the user the resulting effective cap.

The override persists for the rest of the run. It ignores the ramp but still yields to the throttle observer (if 429s are active, the effective cap is still halved).

To clear the override and return to ramp/throttle-only logic: `legion scale <config_max_workers>` (default 5). There isn't a "reset to auto" yet — Phase 4 polish.

---

## Cautions

- Scaling above 3 on Pro-session auth will almost certainly throttle. Watch `/legion-status` for a few minutes after scaling up.
- Scaling DOWN doesn't kill in-flight workers. It just prevents new ones from being spawned until in-flight count drops.
