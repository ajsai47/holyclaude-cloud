"""Unit tests for the CI re-dispatch code path (GitHub issue #10).

Coverage:
  - check_ci() status classification: failed / passed / pending / none
  - fetch_ci_failure() text extraction
  - cmd_reconcile() state transition: ci_failed task → re-queued as pending
    with failure context appended to spec (mocked subprocess + state file)
  - Retry cap: after mediator_max_retries ci re-dispatches the task is marked
    terminal (merge_blocker="ci_failed") rather than re-queued again

No filesystem mocking — the cmd_reconcile tests use a real tmp_path directory
for .legion/state.json so the file-locking in state.update_state() works
correctly.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import reconciler, state
from lib.config import LegionConfig, ReconcilerConfig, ReviewConfig
from lib.state import RunState, Task


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

def _task(
    task_id: str = "T-001",
    pr_url: str = "https://github.com/x/y/pull/42",
    mediator_attempts: int = 0,
    spec: str = "do the thing",
) -> Task:
    return Task(
        id=task_id,
        title="test task",
        spec=spec,
        pr_url=pr_url,
        status="shipped",
        mediator_attempts=mediator_attempts,
    )


def _mock_run(responses: list[tuple[int, str, str]]):
    """Return a MagicMock that yields successive subprocess.CompletedProcess-like
    results.  Each tuple is (returncode, stdout, stderr)."""
    mocks = [
        MagicMock(returncode=rc, stdout=out, stderr=err)
        for rc, out, err in responses
    ]
    return MagicMock(side_effect=mocks)


def _checks_json(checks: list[dict]) -> str:
    return json.dumps(checks)


def _make_run_state(tasks: list[Task]) -> RunState:
    return RunState(
        repo_url="https://github.com/x/y",
        base_branch="main",
        started_at=time.time(),
        tasks={t.id: t for t in tasks},
    )


def _write_state(path: Path, run: RunState) -> None:
    legion_dir = path / ".legion"
    legion_dir.mkdir(parents=True, exist_ok=True)
    state_file = legion_dir / "state.json"
    state_file.write_text(json.dumps(run.to_dict()))


# ---------------------------------------------------------------------------
# check_ci() — status classification
# ---------------------------------------------------------------------------

FAILING_CHECKS = [
    {"name": "lint", "bucket": "fail", "state": "FAILURE"},
    {"name": "build", "bucket": "pass", "state": "SUCCESS"},
]

PASSING_CHECKS = [
    {"name": "lint", "bucket": "pass", "state": "SUCCESS"},
    {"name": "build", "bucket": "pass", "state": "SUCCESS"},
]

PENDING_CHECKS = [
    {"name": "lint", "bucket": "pending", "state": "IN_PROGRESS"},
    {"name": "build", "bucket": "pass", "state": "SUCCESS"},
]

NO_CHECKS: list[dict] = []


def test_check_ci_returns_fail_when_any_check_has_failure_conclusion():
    """check_ci() should return 'fail' when at least one check concluded 'failure'."""
    fake_run = _mock_run([(0, _checks_json(FAILING_CHECKS), "")])
    with patch("lib.reconciler.subprocess.run", fake_run):
        result = reconciler.check_ci("https://github.com/x/y/pull/42")
    assert result == "fail"


def test_check_ci_returns_pass_when_all_checks_succeed():
    """check_ci() should return 'pass' when all checks concluded 'success'."""
    fake_run = _mock_run([(0, _checks_json(PASSING_CHECKS), "")])
    with patch("lib.reconciler.subprocess.run", fake_run):
        result = reconciler.check_ci("https://github.com/x/y/pull/42")
    assert result == "pass"


def test_check_ci_returns_pending_when_checks_still_running():
    """check_ci() should return 'pending' when any check is still in_progress."""
    fake_run = _mock_run([(0, _checks_json(PENDING_CHECKS), "")])
    with patch("lib.reconciler.subprocess.run", fake_run):
        result = reconciler.check_ci("https://github.com/x/y/pull/42")
    assert result == "pending"


def test_check_ci_returns_none_when_no_checks_configured():
    """check_ci() should return 'none' when gh returns an empty array."""
    fake_run = _mock_run([(0, _checks_json(NO_CHECKS), "")])
    with patch("lib.reconciler.subprocess.run", fake_run):
        result = reconciler.check_ci("https://github.com/x/y/pull/42")
    assert result == "none"


def test_check_ci_returns_none_when_pr_url_is_none():
    """check_ci() should return 'none' immediately when pr_url is None,
    without making any subprocess calls."""
    with patch("lib.reconciler.subprocess.run") as mock_run:
        result = reconciler.check_ci(None)
    assert result == "none"
    mock_run.assert_not_called()


def test_check_ci_returns_none_on_non_json_output():
    """check_ci() should return 'none' if gh output cannot be parsed as JSON."""
    fake_run = _mock_run([(0, "not json output", "")])
    with patch("lib.reconciler.subprocess.run", fake_run):
        result = reconciler.check_ci("https://github.com/x/y/pull/42")
    assert result == "none"


def test_check_ci_pending_when_conclusion_is_null():
    """check_ci() should treat queued/in_progress state as pending."""
    checks = [{"name": "ci", "bucket": "pending", "state": "QUEUED"}]
    fake_run = _mock_run([(0, _checks_json(checks), "")])
    with patch("lib.reconciler.subprocess.run", fake_run):
        result = reconciler.check_ci("https://github.com/x/y/pull/42")
    assert result == "pending"


# ---------------------------------------------------------------------------
# fetch_ci_failure() — text extraction
# ---------------------------------------------------------------------------

FAILING_CHECK_LIST = [
    {"name": "lint", "bucket": "fail", "state": "FAILURE",
     "link": "https://github.com/x/y/actions/runs/1/jobs/10"},
    {"name": "typecheck", "bucket": "fail", "state": "FAILURE",
     "link": "https://github.com/x/y/actions/runs/1/jobs/11"},
    {"name": "build", "bucket": "pass", "state": "SUCCESS",
     "link": "https://github.com/x/y/actions/runs/1/jobs/12"},
]


def test_fetch_ci_failure_extracts_failing_check_names():
    """fetch_ci_failure() should list the names of failing checks in the output."""
    fake_run = _mock_run([(0, json.dumps(FAILING_CHECK_LIST), "")])
    with patch("lib.reconciler.subprocess.run", fake_run):
        detail = reconciler.fetch_ci_failure("https://github.com/x/y/pull/42")
    assert "lint" in detail
    assert "typecheck" in detail
    # Passing check should NOT appear as a failure
    assert "build" not in detail or detail.index("build") > detail.index("typecheck")


def test_fetch_ci_failure_includes_check_links():
    """fetch_ci_failure() should include the CI run link for each failing check."""
    fake_run = _mock_run([(0, json.dumps(FAILING_CHECK_LIST), "")])
    with patch("lib.reconciler.subprocess.run", fake_run):
        detail = reconciler.fetch_ci_failure("https://github.com/x/y/pull/42")
    assert "https://github.com/x/y/actions/runs/1/jobs/10" in detail
    assert "https://github.com/x/y/actions/runs/1/jobs/11" in detail


def test_fetch_ci_failure_returns_placeholder_when_no_pr_url():
    """fetch_ci_failure() should return a safe placeholder when pr_url is None."""
    detail = reconciler.fetch_ci_failure(None)
    assert detail  # non-empty
    assert "no pr" in detail.lower() or "none" in detail.lower() or "no pr" in detail.lower()


def test_fetch_ci_failure_returns_placeholder_when_gh_fails():
    """fetch_ci_failure() should return an error message when gh exits non-zero."""
    fake_run = _mock_run([(1, "", "could not find PR")])
    with patch("lib.reconciler.subprocess.run", fake_run):
        detail = reconciler.fetch_ci_failure("https://github.com/x/y/pull/99")
    assert detail  # non-empty
    # Should not raise; should contain some indication of failure
    assert "failed" in detail.lower() or "error" in detail.lower() or "gh" in detail.lower()


def test_fetch_ci_failure_when_no_failing_checks_at_fetch_time():
    """fetch_ci_failure() should handle the case where all checks are passing
    at the time it is called (race between check_ci and fetch)."""
    passing = [{"name": "lint", "bucket": "pass", "state": "SUCCESS", "link": ""}]
    fake_run = _mock_run([(0, json.dumps(passing), "")])
    with patch("lib.reconciler.subprocess.run", fake_run):
        detail = reconciler.fetch_ci_failure("https://github.com/x/y/pull/42")
    assert "no failing" in detail.lower() or "none" in detail.lower() or "not detected" in detail.lower()


# ---------------------------------------------------------------------------
# cmd_reconcile() state transition — ci_failed → re-queued as pending
# ---------------------------------------------------------------------------

def _minimal_cfg(mediator_max_retries: int = 2) -> LegionConfig:
    """Build a LegionConfig with review disabled and a known retry cap."""
    cfg = LegionConfig()
    cfg.reconciler = ReconcilerConfig(mediator_max_retries=mediator_max_retries)
    cfg.review = ReviewConfig(enabled=False)
    return cfg


CI_FAILURE_DETAIL = (
    "The following CI checks failed on the previous attempt's PR:\n"
    "- **lint** — https://github.com/x/y/actions/runs/1/jobs/10\n"
    "\nThe next worker should address these failures before shipping."
)


def _run_reconcile_with_state(tmp_path: Path, run: RunState, cfg: LegionConfig) -> dict:
    """Execute cmd_reconcile against a real state file in tmp_path.

    Returns the parsed JSON output dict (first result entry or the full output).
    """
    import os
    from lib.cli import cmd_reconcile

    # Patch state module to use tmp_path as the working directory so
    # .legion/state.json is created under tmp_path, not the CWD.
    legion_dir = tmp_path / ".legion"
    legion_dir.mkdir(parents=True, exist_ok=True)
    state_file = legion_dir / "state.json"
    state_file.write_text(json.dumps(run.to_dict()))
    lock_file = legion_dir / "state.lock"
    lock_file.touch()

    original_state_path = state.STATE_PATH
    original_lock_path = state.LOCK_PATH
    original_legion_dir = state.LEGION_DIR

    captured_output = []

    def _fake_print(*args, **kwargs):
        if args:
            captured_output.append(str(args[0]))

    state.STATE_PATH = state_file
    state.LOCK_PATH = lock_file
    state.LEGION_DIR = legion_dir
    try:
        with patch("lib.cli.load_config", return_value=cfg), \
             patch("lib.cli.print", side_effect=_fake_print), \
             patch("lib.cli.reconciler.auto_heal", return_value=[]), \
             patch("lib.cli.reviewer.review_pr") as mock_review:
            mock_review.return_value = {"verdict": "clean"}
            args = MagicMock()
            cmd_reconcile(args)
    finally:
        state.STATE_PATH = original_state_path
        state.LOCK_PATH = original_lock_path
        state.LEGION_DIR = original_legion_dir

    # Re-read the (possibly mutated) state
    updated_run = RunState.from_dict(json.loads(state_file.read_text()))
    return updated_run, captured_output


def test_ci_failed_task_is_requeued_as_pending(tmp_path):
    """When reconciler processes a shipped task whose CI fails and
    mediator_attempts < mediator_max_retries, the task is put back to
    'pending' with the CI failure appended to its spec."""
    original_spec = "implement the feature"
    task = _task(spec=original_spec, mediator_attempts=0)
    run = _make_run_state([task])

    cfg = _minimal_cfg(mediator_max_retries=2)

    # Patch check_ci → "fail", fetch_ci_failure → detail text, merge skipped
    with patch("lib.cli.reconciler.check_ci", return_value="fail"), \
         patch("lib.cli.reconciler.fetch_ci_failure", return_value=CI_FAILURE_DETAIL):
        updated_run, _ = _run_reconcile_with_state(tmp_path, run, cfg)

    updated_task = updated_run.tasks["T-001"]
    assert updated_task.status == "pending", (
        f"Expected status='pending', got {updated_task.status!r}"
    )
    assert CI_FAILURE_DETAIL[:100] in updated_task.spec, (
        "CI failure detail should be appended to the task spec"
    )
    assert original_spec in updated_task.spec, (
        "Original spec should be preserved"
    )
    assert "CI failure" in updated_task.spec or "Previous attempt" in updated_task.spec


def test_ci_failed_task_clears_worker_metadata(tmp_path):
    """After a CI re-dispatch the worker-specific fields are cleared so
    spawn treats the task as fresh."""
    task = _task(mediator_attempts=0)
    task.worker_id = "wkr-abc"
    task.branch = "legion/T-001"
    task.dispatched_at = time.time() - 300
    task.finished_at = time.time() - 10
    task.error = "some transient error"
    run = _make_run_state([task])

    cfg = _minimal_cfg(mediator_max_retries=2)

    with patch("lib.cli.reconciler.check_ci", return_value="fail"), \
         patch("lib.cli.reconciler.fetch_ci_failure", return_value=CI_FAILURE_DETAIL):
        updated_run, _ = _run_reconcile_with_state(tmp_path, run, cfg)

    t = updated_run.tasks["T-001"]
    assert t.worker_id is None
    assert t.branch is None
    assert t.pr_url is None
    assert t.dispatched_at is None
    assert t.finished_at is None
    assert t.error is None


def test_ci_failed_task_increments_mediator_attempts(tmp_path):
    """Each CI re-dispatch must increment mediator_attempts by 1."""
    task = _task(mediator_attempts=0)
    run = _make_run_state([task])
    cfg = _minimal_cfg(mediator_max_retries=2)

    with patch("lib.cli.reconciler.check_ci", return_value="fail"), \
         patch("lib.cli.reconciler.fetch_ci_failure", return_value=CI_FAILURE_DETAIL):
        updated_run, _ = _run_reconcile_with_state(tmp_path, run, cfg)

    assert updated_run.tasks["T-001"].mediator_attempts == 1


def test_ci_failed_task_emits_ci_redispatch_event(tmp_path):
    """A 'ci_redispatch' event must be appended to run.events."""
    task = _task(mediator_attempts=0)
    run = _make_run_state([task])
    cfg = _minimal_cfg(mediator_max_retries=2)

    with patch("lib.cli.reconciler.check_ci", return_value="fail"), \
         patch("lib.cli.reconciler.fetch_ci_failure", return_value=CI_FAILURE_DETAIL):
        updated_run, _ = _run_reconcile_with_state(tmp_path, run, cfg)

    redispatch_events = [e for e in updated_run.events if e.get("kind") == "ci_redispatch"]
    assert len(redispatch_events) == 1
    assert redispatch_events[0]["task_id"] == "T-001"
    assert redispatch_events[0]["attempt"] == 1


def test_ci_failed_retry_number_in_spec_header(tmp_path):
    """The spec appendage must include the retry number so the next worker
    can see which attempt it is on."""
    task = _task(mediator_attempts=1, spec="original spec")
    run = _make_run_state([task])
    cfg = _minimal_cfg(mediator_max_retries=3)

    with patch("lib.cli.reconciler.check_ci", return_value="fail"), \
         patch("lib.cli.reconciler.fetch_ci_failure", return_value=CI_FAILURE_DETAIL):
        updated_run, _ = _run_reconcile_with_state(tmp_path, run, cfg)

    spec = updated_run.tasks["T-001"].spec
    # mediator_attempts was 1 before the mutation, so after increment it's 2
    assert "retry #2" in spec or "retry #2" in spec.lower()


# ---------------------------------------------------------------------------
# Retry cap: after mediator_max_retries, task is marked terminal
# ---------------------------------------------------------------------------

def test_ci_failed_at_max_retries_marks_merge_blocker(tmp_path):
    """When mediator_attempts >= mediator_max_retries the task must NOT be
    re-queued — instead merge_blocker is set to 'ci_failed'."""
    # mediator_max_retries=2 means attempts 0 and 1 are allowed; at 2 → blocked
    task = _task(mediator_attempts=2)
    run = _make_run_state([task])
    cfg = _minimal_cfg(mediator_max_retries=2)

    with patch("lib.cli.reconciler.check_ci", return_value="fail"), \
         patch("lib.cli.reconciler.fetch_ci_failure", return_value=CI_FAILURE_DETAIL):
        updated_run, _ = _run_reconcile_with_state(tmp_path, run, cfg)

    t = updated_run.tasks["T-001"]
    assert t.merge_blocker == "ci_failed", (
        f"Expected merge_blocker='ci_failed', got {t.merge_blocker!r}"
    )
    # Status should NOT be reset to pending
    assert t.status == "shipped", (
        f"Status should remain 'shipped' when capped, got {t.status!r}"
    )


def test_ci_failed_at_max_retries_emits_merge_blocked_event(tmp_path):
    """At the retry cap, a 'merge_blocked' event must be appended."""
    task = _task(mediator_attempts=2)
    run = _make_run_state([task])
    cfg = _minimal_cfg(mediator_max_retries=2)

    with patch("lib.cli.reconciler.check_ci", return_value="fail"), \
         patch("lib.cli.reconciler.fetch_ci_failure", return_value=CI_FAILURE_DETAIL):
        updated_run, _ = _run_reconcile_with_state(tmp_path, run, cfg)

    blocked_events = [e for e in updated_run.events if e.get("kind") == "merge_blocked"]
    assert len(blocked_events) == 1
    assert blocked_events[0]["task_id"] == "T-001"
    assert blocked_events[0]["reason"] == "ci_failed"


def test_ci_failed_at_max_retries_spec_is_not_modified(tmp_path):
    """At the retry cap the task spec must not be modified (no failure appended)."""
    original_spec = "original spec content"
    task = _task(mediator_attempts=2, spec=original_spec)
    run = _make_run_state([task])
    cfg = _minimal_cfg(mediator_max_retries=2)

    with patch("lib.cli.reconciler.check_ci", return_value="fail"), \
         patch("lib.cli.reconciler.fetch_ci_failure", return_value=CI_FAILURE_DETAIL):
        updated_run, _ = _run_reconcile_with_state(tmp_path, run, cfg)

    assert updated_run.tasks["T-001"].spec == original_spec


def test_ci_failed_at_max_retries_fetch_ci_failure_not_called(tmp_path):
    """At the retry cap, fetch_ci_failure should not be called — there is
    no point fetching details if we are not re-dispatching."""
    task = _task(mediator_attempts=2)
    run = _make_run_state([task])
    cfg = _minimal_cfg(mediator_max_retries=2)

    with patch("lib.cli.reconciler.check_ci", return_value="fail"), \
         patch("lib.cli.reconciler.fetch_ci_failure", return_value=CI_FAILURE_DETAIL) as mock_fetch:
        _run_reconcile_with_state(tmp_path, run, cfg)

    mock_fetch.assert_not_called()


def test_ci_pending_task_is_skipped_not_redispatched(tmp_path):
    """A task whose CI is still running should be skipped (no state change)."""
    task = _task(mediator_attempts=0)
    run = _make_run_state([task])
    cfg = _minimal_cfg(mediator_max_retries=2)

    with patch("lib.cli.reconciler.check_ci", return_value="pending"), \
         patch("lib.cli.reconciler.fetch_ci_failure") as mock_fetch:
        updated_run, _ = _run_reconcile_with_state(tmp_path, run, cfg)

    t = updated_run.tasks["T-001"]
    assert t.status == "shipped"
    assert t.mediator_attempts == 0
    mock_fetch.assert_not_called()


def test_retry_cap_boundary_last_allowed_retry(tmp_path):
    """mediator_attempts == max_retries - 1 is still within the allowed window:
    the task should be re-dispatched, not blocked."""
    task = _task(mediator_attempts=1)  # max_retries=2, so 1 < 2 → allowed
    run = _make_run_state([task])
    cfg = _minimal_cfg(mediator_max_retries=2)

    with patch("lib.cli.reconciler.check_ci", return_value="fail"), \
         patch("lib.cli.reconciler.fetch_ci_failure", return_value=CI_FAILURE_DETAIL):
        updated_run, _ = _run_reconcile_with_state(tmp_path, run, cfg)

    t = updated_run.tasks["T-001"]
    assert t.status == "pending"
    assert t.merge_blocker is None
