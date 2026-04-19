---
name: legion-cost
description: Show cost summary for the active legion run. Pro-session is free; --api-flagged workers bill against Modal + Anthropic.
argument-hint: ""
---

# /legion-cost

Cost summary for the current run.

---

## Procedure

Run `~/holyclaude-cloud/bin/legion cost` and render the output.

For Phase 2 (Pro-session only), this is mostly $0 — the interesting number is cloud-worker-minutes, which is a leading indicator for when you might start getting throttled.

Phase 4 will integrate the Modal billing API for real `dollars_so_far`, and the Anthropic API cost when `--api` workers ship.

---

## Interpreting the output

- `auth_mode: pro_session` → no Modal GPU / Anthropic API charges; Modal CPU container time is part of your Modal plan's compute budget.
- `cloud_worker_minutes` → sum of wall-time across all cloud workers (finished + in-flight). At ~$0.10-0.50/hr for CPU containers in Modal's paid tier, 60 min = ~$0.10-0.50 of Modal compute.
- `cap_dollars_per_hour: 0` → no $ cap enforced (fine while on Pro session).
