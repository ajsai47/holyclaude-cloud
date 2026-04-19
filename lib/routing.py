"""Dispatcher routing rules. No LLM call — pure heuristics per dispatcher SKILL.md."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import DispatchConfig
from .state import Task


BROWSER_RE = re.compile(r"browser|scrape|playwright", re.IGNORECASE)
THROUGHPUT_RE = re.compile(r"benchmark|measure|profile|long-running", re.IGNORECASE)


@dataclass
class Decision:
    target: str       # "local" | "cloud"
    reason: str


def route(task: Task, dispatch_cfg: DispatchConfig, throttle_active: bool) -> Decision:
    """Per dispatcher SKILL.md, first match wins."""
    title_and_spec = f"{task.title}\n{task.spec}"

    # 1. always_cloud_patterns
    for pat in dispatch_cfg.always_cloud_patterns:
        if re.search(pat, task.title):
            return Decision("cloud", f"matched always-cloud pattern: {pat}")

    # 2. Throttle active + read-only / quick -> local
    if throttle_active:
        if task.estimated_minutes <= 3 and len(task.files_touched) <= 3:
            return Decision("local", "throttle backoff; task is quick enough to stay local")

    # 3. Trivial -> local
    if task.estimated_minutes <= 2 and len(task.files_touched) <= 2:
        return Decision("local", "trivial task")

    # 4. Browser work -> cloud
    if BROWSER_RE.search(title_and_spec):
        return Decision("cloud", "browser work; cloud has chromium")

    # 5. Throughput-bound -> cloud
    if THROUGHPUT_RE.search(title_and_spec):
        return Decision("cloud", "throughput-bound")

    # 6. Small-enough -> local
    if (len(task.files_touched) < dispatch_cfg.local_file_threshold
            and task.estimated_minutes < dispatch_cfg.cloud_minutes_threshold):
        return Decision("local", "small enough for local")

    # 7. Default
    return Decision("cloud", "default route")
