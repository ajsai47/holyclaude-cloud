"""Canonical image definition — DUPLICATED into worker.py.

Modal's default `modal run <file>::fn` does NOT ship sibling .py files
into the container, so `from image import ...` inside worker.py fails
with ModuleNotFoundError. The image definition is therefore inlined
into worker.py, and this file is kept as the documented source of truth.

When you bump HOLYCLAUDE_REF, NODE_MAJOR, PLAYWRIGHT_VERSION, or the
layer structure here, also update the inlined copy at the top of
worker.py. A future refactor could use modal's `add_local_python_source`
to avoid the duplication; see STATUS.md Phase 4.

Layer ordering (cheapest, rarely-changed → most expensive):
  1. debian_slim + python 3.11
  2. apt deps
  3. Node 20
  4. Claude Code CLI
  5. Bun
  6. Python tooling (tomli, httpx, rich)
  7. HolyClaude clone + bun install
  8. Playwright + chromium
  9. GitHub CLI
"""
