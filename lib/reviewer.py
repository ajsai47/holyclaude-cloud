"""Reviewer — pre-merge code review as an adversarial agent.

Called by the reconciler before `gh pr merge`. Spawns a Claude worker
framed as a reviewer, feeds it the task spec + PR diff, asks for a
verdict in strict JSON.

Three possible verdicts:
  - clean     → merge proceeds, no comments
  - warnings  → merge proceeds, reviewer posts issues as a PR comment
  - critical  → merge blocked, task re-dispatched with review feedback
                appended to spec (up to max_review_redispatches)

The reviewer is an independent Claude run — it doesn't share state with
the worker that produced the diff. That's intentional: adversarial review.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from shutil import which

from .config import ReviewConfig
from .state import Task


REVIEW_LOG_ROOT = Path(".legion/review_logs")
REVIEW_VERDICT_JSON_RE = re.compile(r"\{[\s\S]*?\"verdict\"[\s\S]*?\}")


def fetch_pr_diff(pr_url: str | None) -> str:
    """Return the full diff of the PR as a string. Empty string on error."""
    if not pr_url:
        return ""
    num = pr_url.rstrip("/").split("/")[-1]
    result = subprocess.run(
        ["gh", "pr", "diff", num],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout


def _frame_review_prompt(task: Task, diff: str, categories: list[str]) -> str:
    cat_list = ", ".join(categories)
    diff_truncated = diff[:40000] if len(diff) > 40000 else diff
    truncation_note = (
        "\n\n[note: diff truncated at 40KB — reviewer sees first part only]"
        if len(diff) > 40000 else ""
    )

    return (
        f"You are the LEGION REVIEWER for task `{task.id}`. You work independently\n"
        f"from the worker who produced this diff — be rigorous, don't rubber-stamp.\n"
        f"\n"
        f"## Task the worker was given\n"
        f"\n"
        f"**{task.title}**\n"
        f"\n"
        f"{task.spec}\n"
        f"\n"
        f"## Diff of their PR (what actually got written)\n"
        f"\n"
        f"```diff\n"
        f"{diff_truncated}\n"
        f"```{truncation_note}\n"
        f"\n"
        f"## Your review categories\n"
        f"\n"
        f"Review the diff in these categories: **{cat_list}**.\n"
        f"\n"
        f"Brief definitions:\n"
        f"- **security**: SQL injection, command injection, XSS, secret leaks, "
        f"insecure defaults, trust boundary violations, SSRF, path traversal.\n"
        f"- **task_adherence**: does the diff actually implement what the task "
        f"asked for? Did it skip anything? Did it add scope not asked for?\n"
        f"- **silent_failures**: `except:` / `except Exception:` with no log "
        f"and no re-raise; swallowed errors; fallbacks that hide bugs.\n"
        f"- **type_design**: missing types on public APIs (Python/TS), overly "
        f"loose types (`any`, `dict`, `object`), mutable default args.\n"
        f"- **dead_code**: unused imports, unreachable branches, commented-out "
        f"code, added-but-never-called functions.\n"
        f"\n"
        f"## Output format — JSON ONLY\n"
        f"\n"
        f"Output a SINGLE JSON object. No prose before or after. No markdown fences.\n"
        f"\n"
        f"```\n"
        f"{{\n"
        f"  \"verdict\": \"clean\" | \"warnings\" | \"critical\",\n"
        f"  \"summary\": \"one sentence overall assessment\",\n"
        f"  \"issues\": [\n"
        f"    {{\n"
        f"      \"category\": \"one of the categories above\",\n"
        f"      \"severity\": \"critical\" | \"warning\",\n"
        f"      \"file\": \"path/to/file\",\n"
        f"      \"line\": null | N,\n"
        f"      \"message\": \"concrete issue — what's wrong + why\"\n"
        f"    }}\n"
        f"  ]\n"
        f"}}\n"
        f"```\n"
        f"\n"
        f"Verdict rules:\n"
        f"- **critical**: any issue with severity=critical, OR task_adherence "
        f"issues that mean the diff doesn't do what was asked.\n"
        f"- **warnings**: one or more severity=warning issues but nothing "
        f"critical; PR is shippable but worth noting.\n"
        f"- **clean**: no issues found.\n"
        f"\n"
        f"Be concrete. Don't flag style nits. Only flag issues you'd actually\n"
        f"block or comment on in a real code review.\n"
        f"\n"
        f"DO NOT modify any files. DO NOT run tools. DO NOT push or comment.\n"
        f"Just output the JSON verdict.\n"
    )


def _parse_verdict(output: str) -> dict | None:
    """Extract the verdict JSON from the reviewer's output.

    claude-code may emit it inside code fences, alongside thinking, or
    with minor surrounding text. Tolerate all three.
    """
    # 1. Try the last line that looks like pure JSON (most common when using --output-format text)
    # 2. Try finding the first {...} block containing "verdict"
    matches = list(REVIEW_VERDICT_JSON_RE.finditer(output))
    for match in reversed(matches):
        candidate = match.group(0)
        # Greedy extend: if the candidate doesn't have balanced braces, keep going.
        # Our non-greedy regex may have stopped too early. Try rebalancing.
        start = match.start()
        depth = 0
        end = None
        for i, ch in enumerate(output[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end is None:
            continue
        candidate = output[start:end]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def review_pr(task: Task, cfg: ReviewConfig) -> dict:
    """Run the reviewer on this task's PR. Returns {verdict, issues, summary, error?}."""
    if not which("claude"):
        return {
            "verdict": "error",
            "error": "`claude` CLI not on PATH",
        }
    if not task.pr_url:
        return {"verdict": "error", "error": "no pr_url"}

    diff = fetch_pr_diff(task.pr_url)
    if not diff:
        return {"verdict": "error", "error": "empty diff (gh pr diff failed or no changes)"}

    framed = _frame_review_prompt(task, diff, cfg.categories)

    REVIEW_LOG_ROOT.mkdir(parents=True, exist_ok=True)
    log_path = REVIEW_LOG_ROOT / f"{task.id}.log"

    # Run reviewer synchronously — it's fast and the reconciler is already
    # on a tick. Use `--output-format text` so we get clean output to parse.
    with open(log_path, "wb") as log_fh:
        proc = subprocess.run(
            ["claude", "-p", framed,
             "--permission-mode", "bypassPermissions",
             "--output-format", "text"],
            stdout=subprocess.PIPE,
            stderr=log_fh,
            timeout=300,
        )
    output = proc.stdout.decode("utf-8", errors="replace")
    # Also write stdout to the log for debugging
    with open(log_path, "ab") as f:
        f.write(b"\n===== STDOUT =====\n")
        f.write(output.encode("utf-8", errors="replace"))

    if proc.returncode != 0:
        return {
            "verdict": "error",
            "error": f"claude exited rc={proc.returncode}",
            "log": str(log_path),
        }

    verdict = _parse_verdict(output)
    if not verdict:
        return {
            "verdict": "error",
            "error": "could not parse verdict JSON from reviewer output",
            "log": str(log_path),
            "raw_tail": output[-500:],
        }

    # Normalize / validate
    v = verdict.get("verdict", "").lower()
    if v not in ("clean", "warnings", "critical"):
        return {
            "verdict": "error",
            "error": f"reviewer returned invalid verdict {v!r}",
            "log": str(log_path),
        }
    verdict["verdict"] = v
    verdict.setdefault("issues", [])
    verdict.setdefault("summary", "")

    return verdict


def format_issues_for_pr_comment(issues: list[dict]) -> str:
    """Render a list of review issues as a markdown PR comment."""
    if not issues:
        return "_No issues_"
    lines = ["### 🤖 HolyClaude Legion Review", ""]
    by_sev = {"critical": [], "warning": []}
    for i in issues:
        by_sev.setdefault(i.get("severity", "warning"), []).append(i)
    for sev in ("critical", "warning"):
        if not by_sev.get(sev):
            continue
        lines.append(f"**{sev.upper()}**")
        for i in by_sev[sev]:
            loc = ""
            if i.get("file"):
                loc = f" (`{i['file']}"
                if i.get("line"):
                    loc += f":{i['line']}"
                loc += "`)"
            cat = i.get("category", "")
            msg = i.get("message", "")
            lines.append(f"- **[{cat}]**{loc} {msg}")
        lines.append("")
    return "\n".join(lines)


def post_pr_comment(pr_url: str, body: str) -> bool:
    """Post a comment on the PR via gh CLI. Returns True on success."""
    if not pr_url:
        return False
    num = pr_url.rstrip("/").split("/")[-1]
    result = subprocess.run(
        ["gh", "pr", "comment", num, "--body", body],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def format_issues_for_spec(issues: list[dict], summary: str) -> str:
    """Render review feedback to append to a re-dispatched task's spec."""
    lines = [
        "",
        "---",
        f"## Previous attempt's review feedback",
        f"",
        f"A reviewer flagged critical issues with your last PR. Address these before re-shipping:",
        f"",
        f"**Reviewer's summary:** {summary}",
        f"",
    ]
    for i in issues:
        sev = i.get("severity", "warning")
        cat = i.get("category", "")
        loc = ""
        if i.get("file"):
            loc = f" in `{i['file']}`"
            if i.get("line"):
                loc += f":{i['line']}"
        msg = i.get("message", "")
        lines.append(f"- **[{sev.upper()}]** [{cat}]{loc} — {msg}")
    return "\n".join(lines)
