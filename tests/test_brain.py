"""Unit tests for lib.brain — retro store + Retro construction."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.brain import (
    LocalFSBrainStore,
    Retro,
    goal_hash,
    make_retro_from_task,
    repo_slug,
)
from lib.state import Task


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

def test_retro_roundtrip_preserves_all_fields():
    r = Retro(
        task_id="T-1",
        repo_url="https://github.com/x/y.git",
        goal_hash="abc123",
        task_spec_summary="add greeting",
        files_touched=["foo.py", "bar.py"],
        approach_summary="two functions + tests",
        outcome="merged",
        worker_minutes=2.5,
        ci_failed_first_try=True,
        retry_count=1,
        mediator_needed=False,
        review_verdict="warnings",
        review_issues=[{"category": "style", "detail": "missing type hint"}],
        lessons="pytest fixtures beat setUp",
        timestamp=1234567.0,
    )
    r2 = Retro.from_dict(r.to_dict())
    assert r2 == r


def test_retro_from_dict_ignores_unknown_keys():
    # Forward-compat: if a newer version wrote extra fields, we drop them.
    d = {
        "task_id": "T-1",
        "repo_url": "x",
        "goal_hash": "h",
        "task_spec_summary": "s",
        "extra_future_field": "should be ignored",
    }
    r = Retro.from_dict(d)
    assert r.task_id == "T-1"
    assert not hasattr(r, "extra_future_field")


def test_goal_hash_is_deterministic_and_differentiating():
    assert goal_hash("hello") == goal_hash("hello")
    assert goal_hash("hello") != goal_hash("world")
    # Reasonable length — we truncate to 16 for readability
    assert len(goal_hash("x")) == 16


def test_repo_slug_handles_https_and_ssh():
    assert repo_slug("https://github.com/foo/bar.git") == "foo-bar"
    assert repo_slug("https://github.com/foo/bar") == "foo-bar"
    assert repo_slug("https://github.com/foo/bar/") == "foo-bar"
    assert repo_slug("git@github.com:foo/bar.git") == "foo-bar"


# ---------------------------------------------------------------------------
# LocalFSBrainStore — write + roundtrip
# ---------------------------------------------------------------------------

def test_store_write_and_list_roundtrip(tmp_path):
    store = LocalFSBrainStore(tmp_path)
    store.write(Retro(
        task_id="T-1", repo_url="https://github.com/x/y.git",
        goal_hash="abc", task_spec_summary="add greeting",
    ))
    retros = store.list_for_repo("https://github.com/x/y.git")
    assert len(retros) == 1
    assert retros[0].task_id == "T-1"
    assert retros[0].timestamp > 0  # auto-stamped on write


def test_store_list_filters_by_repo(tmp_path):
    store = LocalFSBrainStore(tmp_path)
    store.write(Retro(task_id="T-1", repo_url="https://github.com/a/b.git",
                      goal_hash="g1", task_spec_summary="x"))
    store.write(Retro(task_id="T-2", repo_url="https://github.com/c/d.git",
                      goal_hash="g2", task_spec_summary="y"))
    assert [r.task_id for r in store.list_for_repo("https://github.com/a/b.git")] == ["T-1"]
    assert [r.task_id for r in store.list_for_repo("https://github.com/c/d.git")] == ["T-2"]


def test_store_list_empty_for_unknown_repo(tmp_path):
    store = LocalFSBrainStore(tmp_path)
    assert store.list_for_repo("https://github.com/nothing/here.git") == []


def test_store_list_sorts_most_recent_first(tmp_path):
    store = LocalFSBrainStore(tmp_path)
    store.write(Retro(task_id="T-old", repo_url="https://github.com/x/y.git",
                      goal_hash="g", task_spec_summary="x", timestamp=100.0))
    store.write(Retro(task_id="T-new", repo_url="https://github.com/x/y.git",
                      goal_hash="g", task_spec_summary="y", timestamp=200.0))
    retros = store.list_for_repo("https://github.com/x/y.git")
    assert [r.task_id for r in retros] == ["T-new", "T-old"]


def test_store_skips_corrupt_files(tmp_path):
    store = LocalFSBrainStore(tmp_path)
    # Plant a valid retro + a junk file side-by-side
    store.write(Retro(task_id="T-good", repo_url="https://github.com/x/y.git",
                      goal_hash="g", task_spec_summary="ok"))
    bad = tmp_path / "x-y" / "g" / "corrupt.json"
    bad.write_text("{ not valid json")
    retros = store.list_for_repo("https://github.com/x/y.git")
    assert [r.task_id for r in retros] == ["T-good"]


# ---------------------------------------------------------------------------
# LocalFSBrainStore — search
# ---------------------------------------------------------------------------

def test_search_by_files_touched(tmp_path):
    store = LocalFSBrainStore(tmp_path)
    store.write(Retro(task_id="T-1", repo_url="https://github.com/x/y.git",
                      goal_hash="g", task_spec_summary="a", files_touched=["foo.py"]))
    store.write(Retro(task_id="T-2", repo_url="https://github.com/x/y.git",
                      goal_hash="g", task_spec_summary="b", files_touched=["bar.py"]))
    matches = store.search("https://github.com/x/y.git", files=["foo.py"])
    assert [r.task_id for r in matches] == ["T-1"]


def test_search_by_text_matches_spec_and_lessons_and_approach(tmp_path):
    store = LocalFSBrainStore(tmp_path)
    store.write(Retro(task_id="T-spec", repo_url="https://github.com/x/y.git",
                      goal_hash="g", task_spec_summary="add TYPED utility module"))
    store.write(Retro(task_id="T-lesson", repo_url="https://github.com/x/y.git",
                      goal_hash="g", task_spec_summary="unrelated",
                      lessons="typed dicts beat nested dicts"))
    store.write(Retro(task_id="T-approach", repo_url="https://github.com/x/y.git",
                      goal_hash="g", task_spec_summary="unrelated",
                      approach_summary="pulled out a typed dataclass"))
    store.write(Retro(task_id="T-miss", repo_url="https://github.com/x/y.git",
                      goal_hash="g", task_spec_summary="CI regression"))

    matches = store.search("https://github.com/x/y.git", text="typed")
    assert sorted(r.task_id for r in matches) == ["T-approach", "T-lesson", "T-spec"]


def test_search_by_goal_hash(tmp_path):
    store = LocalFSBrainStore(tmp_path)
    store.write(Retro(task_id="T-a", repo_url="https://github.com/x/y.git",
                      goal_hash="g1", task_spec_summary="x"))
    store.write(Retro(task_id="T-b", repo_url="https://github.com/x/y.git",
                      goal_hash="g2", task_spec_summary="y"))
    matches = store.search("https://github.com/x/y.git", goal_h="g1")
    assert [r.task_id for r in matches] == ["T-a"]


def test_search_respects_limit(tmp_path):
    store = LocalFSBrainStore(tmp_path)
    for i in range(5):
        store.write(Retro(task_id=f"T-{i}", repo_url="https://github.com/x/y.git",
                          goal_hash="g", task_spec_summary="x",
                          timestamp=1000.0 + i))  # non-zero, ascending
    matches = store.search("https://github.com/x/y.git", limit=2)
    assert len(matches) == 2
    # Most recent first
    assert [r.task_id for r in matches] == ["T-4", "T-3"]


# ---------------------------------------------------------------------------
# make_retro_from_task
# ---------------------------------------------------------------------------

def _base_task(**over) -> Task:
    defaults = dict(
        id="T-1", title="test", spec="add a div function",
        files_touched=["math_ops.py"],
    )
    defaults.update(over)
    return Task(**defaults)


def test_make_retro_merged_task_sets_outcome_merged():
    t = _base_task(status="shipped", merged_at=1234.0,
                   dispatched_at=1000.0, finished_at=1060.0)
    r = make_retro_from_task(t, repo_url="https://github.com/x/y.git", goal="add div")
    assert r.outcome == "merged"
    assert r.worker_minutes == 1.0  # 60s / 60


def test_make_retro_blocked_task_preserves_blocker_reason():
    t = _base_task(status="shipped", merge_blocker="needs_human")
    r = make_retro_from_task(t, repo_url="https://github.com/x/y.git", goal="g")
    assert r.outcome == "blocked:needs_human"


def test_make_retro_claude_failed_normalizes_to_failed():
    t = _base_task(status="claude_failed")
    r = make_retro_from_task(t, repo_url="https://github.com/x/y.git", goal="g")
    assert r.outcome == "failed"


def test_make_retro_captures_mediator_and_review_fields():
    t = _base_task(
        status="shipped", merged_at=time.time(),
        mediator_attempts=2, review_verdict="warnings",
        review_issues=[{"category": "security", "detail": "foo"}],
    )
    r = make_retro_from_task(t, repo_url="https://github.com/x/y.git", goal="g")
    assert r.mediator_needed is True
    assert r.review_verdict == "warnings"
    assert r.review_issues == [{"category": "security", "detail": "foo"}]
