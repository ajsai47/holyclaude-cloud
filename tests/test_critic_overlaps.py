"""Unit tests for detect_file_overlaps and detect_orphan_deps in lib/critic.py."""
from __future__ import annotations

import os
import sys

import pytest

# Ensure repo root is importable when tests are run from anywhere.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lib.critic import Flag, detect_file_overlaps, detect_orphan_deps


# ---------------------------------------------------------------------------
# detect_file_overlaps
# ---------------------------------------------------------------------------

def test_no_overlap_returns_empty_list():
    tasks = [
        {"id": "T-001", "deps": [], "files_touched": ["lib/a.py"]},
        {"id": "T-002", "deps": [], "files_touched": ["lib/b.py"]},
    ]
    assert detect_file_overlaps(tasks) == []


def test_siblings_touching_same_file_flags_overlap():
    tasks = [
        {"id": "T-001", "deps": [], "files_touched": ["lib/shared.py"]},
        {"id": "T-002", "deps": [], "files_touched": ["lib/shared.py"]},
    ]
    flags = detect_file_overlaps(tasks)
    assert len(flags) == 1
    flag = flags[0]
    assert isinstance(flag, Flag)
    assert flag.kind == "file_overlap"
    assert flag.severity == "warning"
    assert set(flag.task_ids) == {"T-001", "T-002"}


def test_dependent_tasks_touching_same_file_no_flag():
    tasks = [
        {"id": "T-001", "deps": [], "files_touched": ["lib/shared.py"]},
        {"id": "T-002", "deps": ["T-001"], "files_touched": ["lib/shared.py"]},
    ]
    assert detect_file_overlaps(tasks) == []


# ---------------------------------------------------------------------------
# detect_orphan_deps
# ---------------------------------------------------------------------------

def test_orphan_dep_flags_missing_reference():
    tasks = [
        {"id": "T-001", "deps": ["T-999"], "files_touched": []},
    ]
    flags = detect_orphan_deps(tasks)
    assert len(flags) == 1
    flag = flags[0]
    assert isinstance(flag, Flag)
    assert flag.kind == "orphan_dep"
    assert flag.severity == "critical"
    assert flag.task_ids == ["T-001"]


def test_valid_dep_produces_no_flag():
    tasks = [
        {"id": "T-001", "deps": [], "files_touched": []},
        {"id": "T-002", "deps": ["T-001"], "files_touched": []},
    ]
    assert detect_orphan_deps(tasks) == []
