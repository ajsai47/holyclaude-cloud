"""Unit tests for reconciler merge-state disambiguation."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.reconciler import merge_pr
from lib.state import Task


def _task() -> Task:
    return Task(
        id="T-001",
        title="test",
        spec="",
        pr_url="https://github.com/x/y/pull/1",
        status="shipped",
    )


def _mock_run(responses: list[tuple[int, str, str]]):
    """Build a MagicMock that returns successive subprocess.run results."""
    mocks = [
        MagicMock(returncode=rc, stdout=out, stderr=err)
        for rc, out, err in responses
    ]
    return MagicMock(side_effect=mocks)


# Each merge_pr call path consumes: (optional _pr_is_merged gh call) + gh pr merge + gh pr view mergeStateStatus
# We stub _pr_is_merged to False directly.


def test_merge_pr_returns_needs_human_when_state_is_blocked():
    # gh pr merge fails; mergeStateStatus = BLOCKED → branch protection
    fake_run = _mock_run([
        (1, "", "GraphQL: Pull request is not mergeable"),  # gh pr merge
        (0, json.dumps({"mergeStateStatus": "BLOCKED"}), ""),  # gh pr view
    ])
    with patch("lib.reconciler.subprocess.run", fake_run), \
         patch("lib.reconciler._pr_is_merged", return_value=False):
        result = merge_pr(_task())
    assert result["status"] == "needs_human"
    assert result["merge_state"] == "BLOCKED"


def test_merge_pr_returns_conflict_when_state_is_dirty():
    # gh pr merge fails; mergeStateStatus = DIRTY → real conflict
    fake_run = _mock_run([
        (1, "", "GraphQL: Pull request is not mergeable"),
        (0, json.dumps({"mergeStateStatus": "DIRTY"}), ""),
    ])
    with patch("lib.reconciler.subprocess.run", fake_run), \
         patch("lib.reconciler._pr_is_merged", return_value=False):
        result = merge_pr(_task())
    assert result["status"] == "conflict"
    assert result["merge_state"] == "DIRTY"


def test_merge_pr_returns_ci_blocked_when_state_is_unstable():
    fake_run = _mock_run([
        (1, "", "GraphQL: Pull request is not mergeable"),
        (0, json.dumps({"mergeStateStatus": "UNSTABLE"}), ""),
    ])
    with patch("lib.reconciler.subprocess.run", fake_run), \
         patch("lib.reconciler._pr_is_merged", return_value=False):
        result = merge_pr(_task())
    assert result["status"] == "ci_blocked"
    assert result["merge_state"] == "UNSTABLE"


def test_merge_pr_returns_merged_on_clean_success():
    fake_run = _mock_run([(0, "", "")])  # gh pr merge returns 0
    with patch("lib.reconciler.subprocess.run", fake_run), \
         patch("lib.reconciler._pr_is_merged", return_value=False):
        result = merge_pr(_task())
    assert result["status"] == "merged"


def test_merge_pr_falls_back_to_pattern_match_when_state_unknown():
    # gh pr view fails — legacy error-pattern detection takes over
    fake_run = _mock_run([
        (1, "", "merge conflict detected in file.py"),
        (1, "", "gh view failed"),  # _get_merge_state returns UNKNOWN
    ])
    with patch("lib.reconciler.subprocess.run", fake_run), \
         patch("lib.reconciler._pr_is_merged", return_value=False):
        result = merge_pr(_task())
    assert result["status"] == "conflict"


def test_merge_pr_no_pr_url_short_circuits():
    t = _task()
    t.pr_url = None
    result = merge_pr(t)
    assert result["status"] == "failed"
