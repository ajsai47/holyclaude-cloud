"""Structured retrospectives store — the 'learning brain' (Phase 5C).

Every terminal-state task writes a Retro to the brain. Future tasks and
decompositions can query the brain to see what worked, what didn't, and
what files tripped past workers up.

This module defines:
  - `Retro`: the retrospective record schema
  - `BrainStore`: protocol for any backend (filesystem, Modal volume, SQLite)
  - `LocalFSBrainStore`: filesystem-backed reference implementation
  - `make_retro_from_task`: build a Retro from a terminal-state state.Task

The ModalVolumeBrainStore shipping against Volume
`holyclaude-cloud-shared-brain` is a separate integration step; the
LocalFSBrainStore suffices for local development and testing, and the
file layout is compatible with `modal volume put`/`get` synchronization.

Layout on disk:
  <root>/<repo_slug>/<goal_hash>/<task_id>.json

That shape lets a query scan one repo's retros without touching any
others, and lets two runs of the same goal (same repo, same goal text)
co-locate their retros for easy "what did we do last time?" lookups.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional, Protocol


# ----------------------------------------------------------------------
# Retro schema
# ----------------------------------------------------------------------

@dataclass
class Retro:
    """A single structured retrospective for one task-run."""
    task_id: str
    repo_url: str
    goal_hash: str
    task_spec_summary: str
    files_touched: list[str] = field(default_factory=list)
    approach_summary: str = ""
    outcome: str = ""  # "merged" | "shipped" | "failed" | "no_changes" | "blocked:<reason>"
    worker_minutes: float = 0.0
    ci_failed_first_try: bool = False
    retry_count: int = 0
    mediator_needed: bool = False
    review_verdict: Optional[str] = None  # "clean" | "warnings" | "critical"
    review_issues: list[dict] = field(default_factory=list)
    lessons: str = ""
    timestamp: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Retro":
        # Gracefully ignore unknown keys (forward-compat) and defaults fill missing
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


def goal_hash(goal: str) -> str:
    """Stable 16-char hex hash of the goal text. Used to group retros
    from re-runs of the same goal."""
    return hashlib.sha256(goal.encode("utf-8")).hexdigest()[:16]


def repo_slug(repo_url: str) -> str:
    """Derive a filesystem-safe slug from a git repo URL.

    `https://github.com/foo/bar.git` -> `foo-bar`
    `git@github.com:foo/bar.git`     -> `foo-bar`
    """
    s = repo_url.strip().rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    # Split on : and /, keep last two non-empty segments
    parts = [p for p in s.replace(":", "/").split("/") if p]
    last_two = parts[-2:] if len(parts) >= 2 else parts
    return "-".join(last_two)


# ----------------------------------------------------------------------
# Store interface + implementations
# ----------------------------------------------------------------------

class BrainStore(Protocol):
    def write(self, retro: Retro) -> None: ...
    def list_for_repo(self, repo_url: str) -> list[Retro]: ...
    def search(
        self,
        repo_url: str,
        files: Optional[list[str]] = None,
        text: Optional[str] = None,
        goal_h: Optional[str] = None,
        limit: int = 10,
    ) -> list[Retro]: ...


class LocalFSBrainStore:
    """Filesystem-backed brain. One JSON file per retro.

    Cheap, portable, easy to inspect by hand, `rsync`able to a Modal
    volume. Scans for reads are O(N) over all retros for the repo —
    fine for hundreds of retros per project; revisit if scale hits
    thousands.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, retro: Retro) -> Path:
        return self.root / repo_slug(retro.repo_url) / retro.goal_hash / f"{retro.task_id}.json"

    def write(self, retro: Retro) -> Path:
        if not retro.timestamp:
            retro.timestamp = time.time()
        p = self._path_for(retro)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(retro.to_dict(), indent=2))
        tmp.replace(p)  # atomic
        return p

    def list_for_repo(self, repo_url: str) -> list[Retro]:
        repo_dir = self.root / repo_slug(repo_url)
        if not repo_dir.exists():
            return []
        retros: list[Retro] = []
        for goal_dir in repo_dir.iterdir():
            if not goal_dir.is_dir():
                continue
            for f in goal_dir.glob("*.json"):
                try:
                    retros.append(Retro.from_dict(json.loads(f.read_text())))
                except (json.JSONDecodeError, TypeError):
                    # Corrupt or partial writes are skipped, not fatal.
                    continue
        return sorted(retros, key=lambda r: r.timestamp, reverse=True)

    def search(
        self,
        repo_url: str,
        files: Optional[list[str]] = None,
        text: Optional[str] = None,
        goal_h: Optional[str] = None,
        limit: int = 10,
    ) -> list[Retro]:
        retros = self.list_for_repo(repo_url)
        if goal_h:
            retros = [r for r in retros if r.goal_hash == goal_h]
        if files:
            needle = set(files)
            retros = [r for r in retros if needle.intersection(r.files_touched)]
        if text:
            t = text.lower()
            retros = [
                r for r in retros
                if t in r.task_spec_summary.lower()
                or t in r.lessons.lower()
                or t in r.approach_summary.lower()
            ]
        return retros[:limit]


# ----------------------------------------------------------------------
# Build a Retro from a terminal-state task
# ----------------------------------------------------------------------

def make_retro_from_task(
    task,  # state.Task duck-typed
    repo_url: str,
    goal: str,
    approach_summary: str = "",
    lessons: str = "",
    worker_minutes: Optional[float] = None,
    ci_failed_first_try: bool = False,
) -> Retro:
    """Translate a terminal-state Task + run context into a Retro.

    Status → outcome mapping:
      - merged_at set         → "merged"
      - merge_blocker set     → f"blocked:{merge_blocker}"
      - status == "shipped"   → "shipped" (PR open, not merged)
      - status == "no_changes" → "no_changes"
      - status in ("failed","claude_failed") → "failed"
      - otherwise             → str(status)
    """
    if worker_minutes is None and task.dispatched_at and task.finished_at:
        worker_minutes = max(0.0, (task.finished_at - task.dispatched_at) / 60.0)

    if task.merged_at is not None:
        outcome = "merged"
    elif task.merge_blocker:
        outcome = f"blocked:{task.merge_blocker}"
    elif task.status == "shipped":
        outcome = "shipped"
    elif task.status == "no_changes":
        outcome = "no_changes"
    elif task.status in ("failed", "claude_failed"):
        outcome = "failed"
    else:
        outcome = str(task.status)

    return Retro(
        task_id=task.id,
        repo_url=repo_url,
        goal_hash=goal_hash(goal),
        task_spec_summary=(task.spec or "")[:500],
        files_touched=list(task.files_touched or []),
        approach_summary=approach_summary,
        outcome=outcome,
        worker_minutes=worker_minutes or 0.0,
        ci_failed_first_try=ci_failed_first_try,
        retry_count=getattr(task, "review_attempts", 0),
        mediator_needed=getattr(task, "mediator_attempts", 0) > 0,
        review_verdict=getattr(task, "review_verdict", None),
        review_issues=list(getattr(task, "review_issues", []) or []),
        lessons=lessons,
        timestamp=time.time(),
    )


# ----------------------------------------------------------------------
# Default store factory
# ----------------------------------------------------------------------

DEFAULT_LOCAL_ROOT = Path.home() / ".holyclaude-cloud" / "brain"


def default_store() -> LocalFSBrainStore:
    """The orchestrator's default brain for local dev. Replace with a
    shared-volume-backed store once integration-phase lands."""
    return LocalFSBrainStore(DEFAULT_LOCAL_ROOT)
