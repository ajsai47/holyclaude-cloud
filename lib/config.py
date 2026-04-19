"""legion.toml loader with sane defaults."""
from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


CONFIG_PATH = Path("legion.toml")


@dataclass
class SwarmConfig:
    max_workers: int = 5
    ramp_first_run: bool = True
    human_checkpoint_after_decompose: bool = True
    # "session" (default, Pro rate-limits apply) or "api" (Anthropic API key,
    # costs money but no throttle). Applies to workers, not the orchestrator.
    auth_mode: str = "session"


@dataclass
class BudgetConfig:
    max_dollars_per_hour: float = 0.0
    worker_timeout_minutes: int = 30


@dataclass
class ReconcilerConfig:
    mediator_max_retries: int = 2
    github_repo: str = ""
    branch_prefix: str = "legion/"


@dataclass
class ReviewConfig:
    enabled: bool = True
    categories: list[str] = field(default_factory=lambda: [
        "security",
        "task_adherence",
        "silent_failures",
        "type_design",
        "dead_code",
    ])
    max_review_redispatches: int = 2
    # Where to run the reviewer: "local" | "cloud" | "auto"
    # "auto" = same router as regular workers
    target: str = "local"


@dataclass
class DispatchConfig:
    local_file_threshold: int = 5
    cloud_minutes_threshold: int = 5
    always_cloud_patterns: list[str] = field(default_factory=lambda: [
        r"(?i)scrape",
        r"(?i)browser test",
        r"(?i)long-running",
        r"(?i)benchmark",
    ])


@dataclass
class LegionConfig:
    swarm: SwarmConfig = field(default_factory=SwarmConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    reconciler: ReconcilerConfig = field(default_factory=ReconcilerConfig)
    dispatch: DispatchConfig = field(default_factory=DispatchConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)


def load(path: Path = CONFIG_PATH) -> LegionConfig:
    """Load legion.toml; missing file returns all defaults with a warning."""
    if not path.exists():
        print(
            f"warning: {path} not found, using defaults",
            file=sys.stderr,
        )
        return LegionConfig()

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    return LegionConfig(
        swarm=SwarmConfig(**raw.get("swarm", {})),
        budget=BudgetConfig(**raw.get("budget", {})),
        reconciler=ReconcilerConfig(**raw.get("reconciler", {})),
        dispatch=DispatchConfig(**raw.get("dispatch", {})),
        review=ReviewConfig(**raw.get("review", {})),
    )
