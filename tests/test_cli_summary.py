"""Regression tests for the `legion run` final summary.

Covers GitHub issue #9: `Failed: 0` was printed even when tasks ended in
`claude_failed` or had a `merge_blocker` set. The summary now tallies every
non-success terminal state distinctly and the grand total matches the
number of tasks in state.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.cli import render_run_summary
from lib.state import RunState, Task


def _state(*tasks: Task) -> RunState:
    return RunState(
        tasks={t.id: t for t in tasks},
        events=[],
        repo_url="https://github.com/x/y.git",
        base_branch="main",
        started_at=0.0,
    )


def _run(s: RunState) -> tuple[list[str], int]:
    lines: list[str] = []
    code = render_run_summary(s, lines.append)
    return lines, code


def test_summary_counts_each_terminal_state_distinctly():
    merged = Task(id="M1", title="", spec="", status="shipped", merged_at=1700000000.0,
                  pr_url="https://github.com/x/y/pull/1")
    shipped_open = Task(id="S1", title="", spec="", status="shipped", merged_at=None)
    blocked = Task(id="B1", title="", spec="", status="shipped", merged_at=None,
                   merge_blocker="ci_failed")
    claude_failed = Task(id="F1", title="", spec="", status="claude_failed",
                         error="api 500")
    failed = Task(id="F2", title="", spec="", status="failed", error="worker crash")
    no_changes = Task(id="N1", title="", spec="", status="no_changes")
    cancelled = Task(id="C1", title="", spec="", status="cancelled")
    s = _state(merged, shipped_open, blocked, claude_failed, failed, no_changes, cancelled)

    lines, code = _run(s)
    text = "\n".join(lines)

    # Every category appears on its own line with the correct count.
    assert "Merged:     1" in text
    assert "Shipped:    1" in text
    assert "Blocked:    1" in text
    assert "Failed:     2" in text  # claude_failed + failed
    assert "No changes: 1" in text
    assert "Cancelled:  1" in text

    # Grand total matches len(state.tasks).
    assert f"Total:      {len(s.tasks)}" in text

    # Non-zero exit code since failures/blocked/cancelled exist.
    assert code == 2


def test_summary_tallies_claude_failed_as_failure():
    # Regression: issue #9 — claude_failed tasks were silently dropped from
    # the Failed count (only status == "failed" was counted).
    t = Task(id="T-001", title="", spec="", status="claude_failed",
             error="api error 503")
    s = _state(t)

    lines, code = _run(s)
    text = "\n".join(lines)

    assert "Failed:     1" in text
    assert "T-001" in text
    assert code == 2


def test_summary_tallies_merge_blocker_distinctly_from_failed():
    # Regression: a task with merge_blocker set but status="shipped" used
    # to be counted as Failed in one older line and Merged in another.
    # Blocked now gets its own bucket; Failed does not double-count.
    blocked = Task(id="B1", title="", spec="", status="shipped", merged_at=None,
                   merge_blocker="needs_human")
    s = _state(blocked)

    lines, code = _run(s)
    text = "\n".join(lines)

    assert "Blocked:    1" in text
    assert "Failed:     0" in text
    assert "Merged:     0" in text
    assert "needs_human" in text
    assert code == 2


def test_summary_all_success_returns_zero():
    merged = Task(id="M1", title="", spec="", status="shipped", merged_at=1.0,
                  pr_url="https://github.com/x/y/pull/1")
    no_changes = Task(id="N1", title="", spec="", status="no_changes")
    s = _state(merged, no_changes)

    lines, code = _run(s)
    text = "\n".join(lines)

    assert "Merged:     1" in text
    assert "No changes: 1" in text
    assert "Failed:     0" in text
    assert "Total:      2" in text
    # no_changes is not a failure — clean exit.
    assert code == 0


def test_summary_empty_state_prints_all_zeros():
    s = _state()
    lines, code = _run(s)
    text = "\n".join(lines)

    assert "LEGION RUN COMPLETE" in text
    assert "Merged:     0" in text
    assert "Failed:     0" in text
    assert "Total:      0" in text
    assert code == 0


def test_summary_grand_total_matches_task_count():
    # Property-style: regardless of mix, total == len(tasks).
    tasks = [
        Task(id=f"T{i}", title="", spec="", status=status)
        for i, status in enumerate([
            "shipped", "shipped", "failed", "claude_failed",
            "no_changes", "cancelled", "pending",
        ])
    ]
    tasks[0].merged_at = 1.0  # one shipped is merged
    s = _state(*tasks)

    lines, _ = _run(s)
    text = "\n".join(lines)

    assert f"Total:      {len(tasks)}" in text
