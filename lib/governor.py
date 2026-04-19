"""Governor: dynamic worker cap based on ramp + throttle + cost.

Three signals:
  1. ramp_first_run — start at 1 worker, +1 each time a worker ships clean.
  2. throttle — scan recent worker logs for 429; on hit, halve cap for 10m.
  3. cost — if max_dollars_per_hour > 0 and running-hour spend exceeds it, cap to 0.

The governor is stateless per-call. It derives the current cap from state
+ config each time. No background daemon.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from .config import LegionConfig
from .state import RunState


THROTTLE_RE = re.compile(
    r"(rate[_ ]limit|429|too many requests|usage limit)",
    re.IGNORECASE,
)

THROTTLE_BACKOFF_SECONDS = 10 * 60  # 10 min


def scan_worker_log_for_throttle(log_path: Path) -> bool:
    """Return True if a worker's transcript shows a 429-ish pattern."""
    if not log_path.exists():
        return False
    try:
        text = log_path.read_text(errors="replace")
    except Exception:
        return False
    return bool(THROTTLE_RE.search(text))


def record_throttle(state: RunState) -> None:
    """Start a backoff window. Mutates state (caller holds the lock)."""
    state.throttle_backoff_until = time.time() + THROTTLE_BACKOFF_SECONDS
    state.events.append({
        "ts": time.time(),
        "kind": "throttle_observed",
        "backoff_until": state.throttle_backoff_until,
    })


def throttle_active(state: RunState) -> bool:
    return bool(
        state.throttle_backoff_until
        and time.time() < state.throttle_backoff_until
    )


def shipped_count(state: RunState) -> int:
    return sum(1 for t in state.tasks.values() if t.status == "shipped")


def current_max_workers(state: RunState, config: LegionConfig) -> int:
    """The dynamic cap right now.

    Order of precedence:
      1. /legion-scale override (ignores ramp)
      2. Throttle active  -> halve the hard cap, min 1
      3. Cost breach      -> 0
      4. ramp_first_run   -> 1 + shipped_count, up to max_workers
      5. Config max_workers
    """
    hard_cap = config.swarm.max_workers

    # 1. Manual override
    if state.max_workers_override is not None:
        return max(0, min(state.max_workers_override, hard_cap))

    # 2. Throttle
    if throttle_active(state):
        return max(1, hard_cap // 2)

    # 3. Cost — stub for now; real Modal billing lookup in Phase 4
    if config.budget.max_dollars_per_hour > 0:
        # TODO: query modal billing API; for now assume we're fine
        pass

    # 4. Ramp
    if config.swarm.ramp_first_run:
        ramped = 1 + shipped_count(state)
        return min(ramped, hard_cap)

    # 5. Default
    return hard_cap


def stale_in_flight(state: RunState, config: LegionConfig) -> list[str]:
    """Task IDs that have been in-flight longer than worker_timeout_minutes."""
    now = time.time()
    cutoff = now - (config.budget.worker_timeout_minutes * 60)
    stale = []
    for t in state.tasks.values():
        if t.status == "in_flight" and t.dispatched_at and t.dispatched_at < cutoff:
            stale.append(t.id)
    return stale
