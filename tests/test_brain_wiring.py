"""Tests for brain read-on-spawn and write-on-finish wiring."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lib.brain import Retro, LocalFSBrainStore, make_retro_from_task, goal_hash
from lib.dispatch import frame_prompt
from lib.state import Task


# ---------------------------------------------------------------------------
# frame_prompt — retro injection
# ---------------------------------------------------------------------------

def _make_task(task_id="T-001", title="Add auth", spec="Implement JWT auth", files=None):
    return Task(
        id=task_id, title=title, spec=spec,
        files_touched=files or ["lib/auth.py"],
    )


def test_frame_prompt_no_retros_returns_base():
    task = _make_task()
    result = frame_prompt(task, "main", "legion/T-001")
    assert "Past experience" not in result
    assert task.spec in result


def test_frame_prompt_with_retros_appends_section():
    task = _make_task()
    retro = Retro(
        task_id="T-000", repo_url="https://github.com/foo/bar",
        goal_hash="abc123", task_spec_summary="Add login endpoint",
        files_touched=["lib/auth.py"],
        outcome="merged", lessons="Use the existing middleware pattern.",
        timestamp=time.time() - 3600,
    )
    result = frame_prompt(task, "main", "legion/T-001", retros=[retro])
    assert "Past experience" in result
    assert "T-000" in result
    assert "merged" in result
    assert "Use the existing middleware pattern." in result


def test_frame_prompt_caps_retros_at_five():
    task = _make_task()
    retros = [
        Retro(
            task_id=f"T-{i:03d}", repo_url="https://github.com/foo/bar",
            goal_hash="abc", task_spec_summary=f"task {i}",
            outcome="merged", timestamp=time.time() - i * 60,
        )
        for i in range(10)
    ]
    result = frame_prompt(task, "main", "legion/T-001", retros=retros)
    # Only first 5 should appear
    assert "T-000" in result
    assert "T-004" in result
    assert "T-005" not in result


def test_frame_prompt_retro_includes_review_issues():
    task = _make_task()
    retro = Retro(
        task_id="T-099", repo_url="https://github.com/foo/bar",
        goal_hash="abc", task_spec_summary="Fix caching",
        outcome="merged",
        review_issues=[{
            "severity": "critical", "category": "security",
            "message": "SQL injection in query builder", "file": "lib/db.py", "line": 42,
        }],
        timestamp=time.time() - 120,
    )
    result = frame_prompt(task, "main", "legion/T-001", retros=[retro])
    assert "SQL injection" in result
    assert "security" in result


def test_frame_prompt_empty_retros_list_no_section():
    task = _make_task()
    result = frame_prompt(task, "main", "legion/T-001", retros=[])
    assert "Past experience" not in result


# ---------------------------------------------------------------------------
# _flush_brain_retros — write-on-finish
# ---------------------------------------------------------------------------

def _make_run_state(tasks, repo_url="https://github.com/foo/bar"):
    s = MagicMock()
    s.repo_url = repo_url
    s.tasks = {t.id: t for t in tasks}
    return s


def test_flush_brain_retros_writes_merged_task(tmp_path):
    from lib.cli import _flush_brain_retros, _brain_written_ids
    _brain_written_ids.clear()

    task = _make_task("T-001")
    task.merged_at = time.time()
    task.status = "shipped"
    s = _make_run_state([task])

    import lib.brain as _brain_mod
    orig = _brain_mod.DEFAULT_LOCAL_ROOT
    _brain_mod.DEFAULT_LOCAL_ROOT = tmp_path / "brain"
    store = LocalFSBrainStore(tmp_path / "brain")
    try:
        _flush_brain_retros(s, goal="Add auth")
    finally:
        _brain_mod.DEFAULT_LOCAL_ROOT = orig

    written = store.list_for_repo(s.repo_url)
    assert len(written) == 1
    assert written[0].task_id == "T-001"
    assert written[0].outcome == "merged"


def test_flush_brain_retros_skips_in_flight_task(tmp_path):
    from lib.cli import _flush_brain_retros, _brain_written_ids
    _brain_written_ids.clear()

    task = _make_task("T-002")
    task.status = "in_flight"
    task.merged_at = None
    s = _make_run_state([task])

    import lib.brain as _brain_mod
    orig = _brain_mod.DEFAULT_LOCAL_ROOT
    _brain_mod.DEFAULT_LOCAL_ROOT = tmp_path / "brain"
    store = LocalFSBrainStore(tmp_path / "brain")
    try:
        _flush_brain_retros(s, goal="test goal")
    finally:
        _brain_mod.DEFAULT_LOCAL_ROOT = orig

    assert store.list_for_repo(s.repo_url) == []


def test_flush_brain_retros_skips_already_written(tmp_path):
    from lib.cli import _flush_brain_retros, _brain_written_ids
    _brain_written_ids.clear()

    task = _make_task("T-003")
    task.merged_at = time.time()
    task.status = "shipped"
    s = _make_run_state([task])

    import lib.brain as _brain_mod
    orig = _brain_mod.DEFAULT_LOCAL_ROOT
    _brain_mod.DEFAULT_LOCAL_ROOT = tmp_path / "brain"
    store = LocalFSBrainStore(tmp_path / "brain")
    try:
        _flush_brain_retros(s, goal="test goal")
        _flush_brain_retros(s, goal="test goal")  # second call should be no-op
    finally:
        _brain_mod.DEFAULT_LOCAL_ROOT = orig

    # Should only have written once
    assert len(store.list_for_repo(s.repo_url)) == 1
