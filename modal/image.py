"""The meta-on-meta image: every cloud worker is a full HolyClaude install.

Bakes in:
  - Node 20 + @anthropic-ai/claude-code
  - Bun (for browse + autoloop)
  - Playwright + chromium (so cloud workers can /browse)
  - tree-sitter parsers (smart-explore)
  - HolyClaude cloned at a pinned ref
  - claude-mem MCP server

Cached layers — order matters. Earliest layers change least often.
"""
from __future__ import annotations

import modal


# Pin HolyClaude to a known-good commit. Bump when you want workers to upgrade.
HOLYCLAUDE_REPO = "https://github.com/ajsai47/holyclaude.git"
HOLYCLAUDE_REF = "main"  # TODO: pin to a SHA before any production use

# Node and Playwright versions
NODE_MAJOR = "20"
PLAYWRIGHT_VERSION = "1.48.0"


image = (
    modal.Image.debian_slim(python_version="3.11")
    # Layer 1: OS deps (rare changes)
    .apt_install(
        "curl", "git", "ca-certificates", "gnupg", "build-essential",
        "unzip", "jq",
        # Playwright/Chromium runtime deps
        "libnss3", "libatk1.0-0", "libatk-bridge2.0-0", "libcups2",
        "libdrm2", "libxkbcommon0", "libxcomposite1", "libxdamage1",
        "libxrandr2", "libgbm1", "libpango-1.0-0", "libcairo2", "libasound2",
    )
    # Layer 2: Node 20
    .run_commands(
        f"curl -fsSL https://deb.nodesource.com/setup_{NODE_MAJOR}.x | bash -",
        "apt-get install -y nodejs",
    )
    # Layer 3: Claude Code CLI
    .run_commands(
        "npm install -g @anthropic-ai/claude-code",
    )
    # Layer 4: Bun
    .run_commands(
        "curl -fsSL https://bun.sh/install | bash",
        # bun installs to ~/.bun/bin; make it available to all shells in the container
        "ln -s /root/.bun/bin/bun /usr/local/bin/bun",
    )
    # Layer 5: Python deps for governor / cost meter
    .pip_install(
        "tomli==2.0.2",
        "httpx==0.27.2",
        "rich==13.9.4",
    )
    # Layer 6: Clone HolyClaude. This is the meta-on-meta moment.
    # Workers get the full plugin: memory, browser, team, autoloop.
    .run_commands(
        f"git clone {HOLYCLAUDE_REPO} /opt/holyclaude",
        f"cd /opt/holyclaude && git checkout {HOLYCLAUDE_REF}",
        # HolyClaude's setup script — installs deps, builds browse, etc.
        # We run it but skip Claude-Code-restart-required steps.
        "cd /opt/holyclaude && bun install --frozen-lockfile || bun install",
    )
    # Layer 7: Playwright browsers (chromium only — saves ~500MB vs all)
    .run_commands(
        f"npm install -g playwright@{PLAYWRIGHT_VERSION}",
        "npx playwright install chromium --with-deps",
    )
    # Layer 8: GitHub CLI for PR creation
    .run_commands(
        "curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | "
        "  dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg",
        "chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg",
        'echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] '
        'https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list',
        "apt-get update",
        "apt-get install -y gh",
    )
    # Layer 9 (last): worker code from THIS repo. Changes most often.
    # Caller will .add_local_dir() this layer at function-build time.
)


# Volumes
# - shared-brain: claude-mem SQLite DB shared across all workers
# - worker-cache: ephemeral per-task caches (one subdirectory per task-id)
SHARED_BRAIN_VOLUME = "holyclaude-cloud-shared-brain"
WORKER_CACHE_VOLUME = "holyclaude-cloud-worker-cache"

shared_brain = modal.Volume.from_name(SHARED_BRAIN_VOLUME, create_if_missing=True)
worker_cache = modal.Volume.from_name(WORKER_CACHE_VOLUME, create_if_missing=True)


# Secrets the workers need
# - claude-pro-session: created by holyclaude-cloud setup script from local credentials
# - legion-github: GitHub PAT with repo scope, for cloning private repos and opening PRs
SECRETS = [
    modal.Secret.from_name("claude-pro-session"),
    modal.Secret.from_name("legion-github"),
]
