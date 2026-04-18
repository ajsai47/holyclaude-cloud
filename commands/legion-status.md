---
name: legion-status
description: Show the current state of an active HolyClaude legion run.
argument-hint: ""
---

# /legion-status

What's the swarm doing right now.

---

## Procedure

1. Read `./.legion/state.json`. If missing, say "No active legion run in this repo."
2. Render a status table:

```
LEGION STATUS — repo: <repo-name>  started: <iso-timestamp>  elapsed: <Xm Ys>

In flight (N):
  T-003  cloud   Add OAuth callback handler          12m 04s
  T-004  local   Update README                        00m 47s

Ready queue (M):
  T-005  cloud   Add token refresh logic
  T-007  local   Wire OAuth into auth middleware

Shipped (K):
  T-001  https://github.com/.../pull/142
  T-002  https://github.com/.../pull/143

Failed (J):
  T-006  reason: see .legion/blockers/T-006.md

Throttle: clean   Workers: 2/5   Cost: $0 (Pro session)
```

3. If any in-flight cloud worker has been running >`worker_timeout_minutes`, flag it red — the Modal function will time out soon.

4. Tail the most recent activity from `.legion/transcripts/` for whichever in-flight task the user names, if they ask.
