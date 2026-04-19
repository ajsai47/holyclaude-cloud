---
name: legion-reconcile
description: Merge shipped legion PRs in dependency order, invoking the mediator on conflicts. Safe to run repeatedly.
argument-hint: "[--loop]"
---

# /legion-reconcile

Drain the merge queue.

---

## Load skill

Load the `legion-reconciler` skill. Read its flow description before invoking.

---

## Parse arguments

$ARGUMENTS

- `--loop`: keep calling `legion reconcile` every 20s until no ready tasks remain (useful after a big dispatch has finished).
- default: one pass.

---

## Procedure

**One-pass** (default):

```bash
~/holyclaude-cloud/bin/legion reconcile
```

Render the output to the user. Call out:
- Each task that merged (show PR URL + that it merged)
- Any task that got `mediated_and_merged` — mediator actually did something
- Any `ci_failed` or `mediator_maxed` tasks — surface with next steps

**Loop** (`--loop`):

```
while true:
  result=$(~/holyclaude-cloud/bin/legion reconcile)
  if echo "$result" | grep -q '"action": "idle"'; then
    echo "queue drained"
    break
  fi
  # Also break if nothing changed in the last two passes (avoid infinite
  # spin when stuck on ci_pending).
  sleep 20
done
```

End with a status summary from `legion status`.

---

## When to use

- After a big dispatch run where many PRs are shipped but not yet merged.
- If CI takes time and you want to come back and flush.
- To manually resolve a known conflict: `legion mediate <task-id>` then `legion reconcile` again.
