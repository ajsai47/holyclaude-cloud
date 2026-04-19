"""Legion run state, file-locked.

All mutations go through update_state() which:
  1. Acquires an exclusive flock on .legion/state.lock
  2. Reads state.json
  3. Calls the caller's mutation fn
  4. Writes state.json atomically (tempfile + rename)
  5. Releases the lock

Safe against concurrent `legion` CLI invocations (e.g. the orchestrator
polling while the user runs /legion-scale).
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator


LEGION_DIR = Path(".legion")
STATE_PATH = LEGION_DIR / "state.json"
LOCK_PATH = LEGION_DIR / "state.lock"
STOP_PATH = LEGION_DIR / "STOP"


# ----------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------

# Task statuses, in rough lifecycle order.
# pending: decomposed, not yet dispatched
# ready:   deps satisfied, waiting for a worker slot
# in_flight: worker running
# shipped: PR opened successfully
# no_changes: worker finished but produced no diff
# failed: worker errored (claude_failed or blocker)
# cancelled: stop signal hit before dispatch
TASK_STATUSES = {
    "pending", "ready", "in_flight",
    "shipped", "no_changes", "failed", "cancelled",
}


@dataclass
class Task:
    id: str
    title: str
    spec: str
    deps: list[str] = field(default_factory=list)
    estimated_minutes: int = 10
    files_touched: list[str] = field(default_factory=list)

    # Runtime fields
    status: str = "pending"
    target: str | None = None           # "local" | "cloud"
    worker_id: str | None = None        # modal call id OR local pid
    branch: str | None = None
    pr_url: str | None = None
    dispatched_at: float | None = None  # unix ts
    finished_at: float | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        return cls(**d)


@dataclass
class RunState:
    repo_url: str
    base_branch: str
    started_at: float
    tasks: dict[str, Task] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)  # audit log
    max_workers_override: int | None = None  # from /legion-scale
    throttle_backoff_until: float | None = None

    def to_dict(self) -> dict:
        return {
            "repo_url": self.repo_url,
            "base_branch": self.base_branch,
            "started_at": self.started_at,
            "tasks": {tid: t.to_dict() for tid, t in self.tasks.items()},
            "events": self.events,
            "max_workers_override": self.max_workers_override,
            "throttle_backoff_until": self.throttle_backoff_until,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RunState":
        return cls(
            repo_url=d["repo_url"],
            base_branch=d["base_branch"],
            started_at=d["started_at"],
            tasks={tid: Task.from_dict(t) for tid, t in d.get("tasks", {}).items()},
            events=d.get("events", []),
            max_workers_override=d.get("max_workers_override"),
            throttle_backoff_until=d.get("throttle_backoff_until"),
        )


# ----------------------------------------------------------------------
# Locking
# ----------------------------------------------------------------------

@contextlib.contextmanager
def _exclusive_lock() -> Iterator[None]:
    """flock-based exclusive lock on .legion/state.lock."""
    LEGION_DIR.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.touch(exist_ok=True)
    with open(LOCK_PATH, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------

def exists() -> bool:
    return STATE_PATH.exists()


def init_run(
    repo_url: str,
    base_branch: str,
    tasks: list[Task],
) -> RunState:
    """Create a new .legion/state.json. Errors if one already exists."""
    with _exclusive_lock():
        if STATE_PATH.exists():
            raise RuntimeError(
                f"{STATE_PATH} already exists. Run `legion stop --force` to clear."
            )
        state = RunState(
            repo_url=repo_url,
            base_branch=base_branch,
            started_at=time.time(),
            tasks={t.id: t for t in tasks},
        )
        _write(state)
        _append_event(state, "run_init", {"task_count": len(tasks)})
    return state


def read_state() -> RunState:
    with _exclusive_lock():
        return _read()


def update_state(mutator: Callable[[RunState], None]) -> RunState:
    """Apply mutator(state) under the lock, persist."""
    with _exclusive_lock():
        state = _read()
        mutator(state)
        _write(state)
    return state


def stop_requested() -> str | None:
    """Returns 'graceful' | 'force' | None based on the STOP file."""
    if not STOP_PATH.exists():
        return None
    content = STOP_PATH.read_text().strip()
    if content.startswith("force"):
        return "force"
    return "graceful"


def write_stop(mode: str) -> None:
    assert mode in ("graceful", "force")
    LEGION_DIR.mkdir(parents=True, exist_ok=True)
    STOP_PATH.write_text(f"{mode}\n{time.time()}\n")


def clear_stop() -> None:
    STOP_PATH.unlink(missing_ok=True)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def ready_tasks(state: RunState) -> list[Task]:
    """Tasks whose deps are all shipped (or no_changes — counts as complete)."""
    done = {
        tid for tid, t in state.tasks.items()
        if t.status in ("shipped", "no_changes")
    }
    return [
        t for t in state.tasks.values()
        if t.status == "pending" and all(d in done for d in t.deps)
    ]


def in_flight_tasks(state: RunState) -> list[Task]:
    return [t for t in state.tasks.values() if t.status == "in_flight"]


def _append_event(state: RunState, kind: str, data: dict) -> None:
    state.events.append({
        "ts": time.time(),
        "kind": kind,
        **data,
    })


def log_event(kind: str, **data) -> None:
    """Top-level convenience: append an event under the lock."""
    def mut(s: RunState):
        _append_event(s, kind, data)
    update_state(mut)


# ----------------------------------------------------------------------
# Private I/O
# ----------------------------------------------------------------------

def _read() -> RunState:
    if not STATE_PATH.exists():
        raise RuntimeError(f"No legion run in this repo ({STATE_PATH} missing).")
    with open(STATE_PATH) as f:
        return RunState.from_dict(json.load(f))


def _write(state: RunState) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state.to_dict(), f, indent=2, sort_keys=True)
    os.replace(tmp, STATE_PATH)
