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

# Diff size limits. Hard cap is what we send to the reviewer; over this
# we do a head+tail cut so the reviewer sees both ends. Warnings are
# forced on any truncated review regardless of reviewer's stated verdict
# (other than critical, which we honor).
DIFF_MAX_BYTES = 40_000
DIFF_HEAD_BYTES = 20_000
DIFF_TAIL_BYTES = 18_000


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


def _frame_review_prompt(task: Task, diff: str, categories: list[str]) -> tuple[str, bool]:
    """Returns (framed_prompt, was_truncated)."""
    cat_list = ", ".join(categories)
    was_truncated = False
    if len(diff) > DIFF_MAX_BYTES:
        was_truncated = True
        head = diff[:DIFF_HEAD_BYTES]
        tail = diff[-DIFF_TAIL_BYTES:]
        omitted = len(diff) - DIFF_HEAD_BYTES - DIFF_TAIL_BYTES
        diff_display = (
            f"{head}\n\n"
            f"... [MIDDLE OF DIFF OMITTED: {omitted:,} bytes, "
            f"~{omitted // 50:,} lines not shown. Flag this in your verdict.] ...\n\n"
            f"{tail}"
        )
        truncation_note = (
            f"\n\n**IMPORTANT: THIS DIFF WAS TRUNCATED.** "
            f"Original size {len(diff):,} bytes, you see "
            f"{DIFF_HEAD_BYTES + DIFF_TAIL_BYTES:,} bytes (head + tail). "
            f"You cannot verify the middle. Your verdict should reflect this: "
            f"default to `warnings` with a note about unreviewable content, "
            f"unless you find a critical issue in what you CAN see."
        )
    else:
        diff_display = diff
        truncation_note = ""

    prompt = (
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
        f"{diff_display}\n"
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
    return prompt, was_truncated


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

    framed, was_truncated = _frame_review_prompt(task, diff, cfg.categories)

    REVIEW_LOG_ROOT.mkdir(parents=True, exist_ok=True)
    log_path = REVIEW_LOG_ROOT / f"{task.id}.log"

    # Run reviewer synchronously — it's fast and the reconciler is already
    # on a tick. One retry on failure/timeout (common causes: transient
    # auth refresh, rate-limit blip, gh API flake).
    attempt = 0
    last_error = None
    output = ""
    while attempt < 2:
        attempt += 1
        try:
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
            with open(log_path, "ab") as f:
                f.write(f"\n===== STDOUT (attempt {attempt}) =====\n".encode())
                f.write(output.encode("utf-8", errors="replace"))
            if proc.returncode == 0:
                break
            last_error = f"claude exited rc={proc.returncode} on attempt {attempt}"
        except subprocess.TimeoutExpired:
            last_error = f"claude timed out (300s) on attempt {attempt}"
            with open(log_path, "ab") as f:
                f.write(f"\n===== TIMEOUT on attempt {attempt} =====\n".encode())

    if not output:
        return {
            "verdict": "error",
            "error": last_error or "no output from reviewer",
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

    # Conservative policy: truncated diffs can't be cleanly "clean" —
    # the reviewer literally didn't see all of it. Downgrade to warnings
    # (honor `critical` though; the reviewer found something concrete).
    if was_truncated and v == "clean":
        verdict["verdict"] = "warnings"
        verdict["issues"] = list(verdict.get("issues", [])) + [{
            "category": "dead_code",
            "severity": "warning",
            "file": None,
            "line": None,
            "message": (
                f"Diff was truncated from {len(diff):,} bytes to "
                f"{DIFF_HEAD_BYTES + DIFF_TAIL_BYTES:,} bytes (head + tail). "
                "Reviewer saw both ends but not the middle. Verdict "
                "downgraded from 'clean' to 'warnings' as a safety "
                "default — human should spot-check the unreviewed region."
            ),
        }]
        verdict["truncated"] = True

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
