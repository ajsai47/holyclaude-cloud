"""Unit tests for lib.state helpers."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.state import RunState, Task, ready_tasks


def _state(*tasks: Task) -> RunState:
    return RunState(
        tasks={t.id: t for t in tasks},
        events=[],
        repo_url="https://github.com/x/y.git",
        base_branch="main",
        started_at=0.0,
    )


def test_ready_tasks_shipped_dep_not_yet_merged_is_not_ready():
    # Regression: dispatcher used to spawn dependents on "shipped" status,
    # but shipped means "PR opened" not "merged into main" — a downstream
    # worker would clone main without the dep's code.
    dep = Task(id="A", title="", spec="", deps=[], status="shipped", merged_at=None)
    downstream = Task(id="B", title="", spec="", deps=["A"], status="pending")
    s = _state(dep, downstream)
    assert ready_tasks(s) == []


def test_ready_tasks_merged_dep_makes_downstream_ready():
    dep = Task(id="A", title="", spec="", deps=[], status="shipped", merged_at=1700000000.0)
    downstream = Task(id="B", title="", spec="", deps=["A"], status="pending")
    s = _state(dep, downstream)
    assert [t.id for t in ready_tasks(s)] == ["B"]


def test_ready_tasks_no_changes_dep_counts_as_complete():
    # A task that finished with no diff has nothing to merge but its intent
    # is satisfied — downstream should be dispatchable.
    dep = Task(id="A", title="", spec="", deps=[], status="no_changes", merged_at=None)
    downstream = Task(id="B", title="", spec="", deps=["A"], status="pending")
    s = _state(dep, downstream)
    assert [t.id for t in ready_tasks(s)] == ["B"]


def test_ready_tasks_root_task_with_no_deps_is_ready():
    root = Task(id="A", title="", spec="", deps=[], status="pending")
    s = _state(root)
    assert [t.id for t in ready_tasks(s)] == ["A"]


def test_ready_tasks_failed_dep_blocks_downstream():
    dep = Task(id="A", title="", spec="", deps=[], status="failed", merged_at=None)
    downstream = Task(id="B", title="", spec="", deps=["A"], status="pending")
    s = _state(dep, downstream)
    assert ready_tasks(s) == []
