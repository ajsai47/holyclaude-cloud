"""Unit tests for deterministic critic checks."""
from __future__ import annotations

import sys
from pathlib import Path

# Make the repo root importable so `lib.critic` resolves when pytest is run from
# inside tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.critic import Flag, detect_dep_cycles, detect_oversized, detect_weak_specs


def _task(tid: str, deps: list[str] | None = None, spec: str = "x" * 80,
          estimated_minutes: int = 30) -> dict:
    return {
        "id": tid,
        "deps": deps or [],
        "spec": spec,
        "estimated_minutes": estimated_minutes,
    }


# ---------------------------------------------------------------------------
# detect_dep_cycles
# ---------------------------------------------------------------------------

def test_detect_dep_cycles_no_cycle_returns_empty():
    tasks = [
        _task("A", deps=[]),
        _task("B", deps=["A"]),
        _task("C", deps=["A", "B"]),
    ]
    assert detect_dep_cycles(tasks) == []


def test_detect_dep_cycles_mutual_dependency_flags_both_tasks():
    tasks = [
        _task("A", deps=["B"]),
        _task("B", deps=["A"]),
    ]
    flags = detect_dep_cycles(tasks)

    assert len(flags) == 2
    assert all(isinstance(f, Flag) for f in flags)
    assert all(f.kind == "dep_cycle" for f in flags)
    assert all(f.severity == "critical" for f in flags)
    assert any(f.task_ids == ["A"] for f in flags)
    assert any(f.task_ids == ["B"] for f in flags)


def test_detect_dep_cycles_self_reference_flags_task():
    tasks = [_task("A", deps=["A"])]
    flags = detect_dep_cycles(tasks)

    assert len(flags) == 1
    flag = flags[0]
    assert isinstance(flag, Flag)
    assert flag.kind == "dep_cycle"
    assert flag.severity == "critical"
    assert flag.task_ids == ["A"]


# ---------------------------------------------------------------------------
# detect_weak_specs
# ---------------------------------------------------------------------------

def test_detect_weak_specs_short_spec_flagged():
    short_spec = "too short"  # 9 chars, well under 40
    assert len(short_spec) < 40
    tasks = [_task("A", spec=short_spec)]
    flags = detect_weak_specs(tasks)

    assert len(flags) == 1
    flag = flags[0]
    assert isinstance(flag, Flag)
    assert flag.kind == "weak_spec"
    assert flag.severity == "warning"
    assert flag.task_ids == ["A"]


def test_detect_weak_specs_long_spec_no_flag():
    long_spec = "x" * 40  # exactly at threshold — no flag (strictly less-than)
    assert len(long_spec) >= 40
    tasks = [_task("A", spec=long_spec)]
    assert detect_weak_specs(tasks) == []


# ---------------------------------------------------------------------------
# detect_oversized
# ---------------------------------------------------------------------------

def test_detect_oversized_over_cap_flagged():
    tasks = [_task("A", estimated_minutes=120)]
    flags = detect_oversized(tasks)

    assert len(flags) == 1
    flag = flags[0]
    assert isinstance(flag, Flag)
    assert flag.kind == "oversized_task"
    assert flag.severity == "warning"
    assert flag.task_ids == ["A"]


def test_detect_oversized_under_cap_no_flag():
    tasks = [_task("A", estimated_minutes=30)]
    assert detect_oversized(tasks) == []
