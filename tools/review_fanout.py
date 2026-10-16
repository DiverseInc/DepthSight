#!/usr/bin/env python3
"""
One-command adversarial review: fan the current diff out to five models.

Why this is a script and not a habit
------------------------------------
Five separate shell invocations, each with its own invocation quirk, is a
workflow that fails intermittently. On 2026-10-16 two of five reviewers were
run wrong before any of them produced a verdict. Encoding the quirks once means
the fallback paths stop being folklore.

Invocation quirks this encodes (all learned the hard way)
--------------------------------------------------------
* opencode : the message goes BEFORE ``-f``. ``-f`` is an array flag, and the
  reversed order silently treats the prompt as a filename. The file MUST be
  UTF-8 -- PowerShell's ``>`` writes UTF-16 and opencode then reads it as
  binary.
* hermes   : ``hermes -z``. There is no ``hermes run``. It refuses the system
  temp dir, so the scratch dir is pinned.
* dsh      : ``dsh headless``. The MCP web harness is for interactive browser
  work and is useless for a one-shot review.
* claude   : ``claude -p``.
* ollama   : the CLI, not the MCP tool. The MCP tool defaults to a model that
  is not pulled on this box and errors; ``qwen2.5-coder:7b`` is present.

The rule that matters more than any of the above
------------------------------------------------
Narrow the question. OpenCode burned 900s on "is this fix safe?" and returned
NO verdict, which is NOT approval. Re-asked as one question with a 20-line
cap, it answered in minutes. Every prompt below is a single question.

And: never run this while something is mutating the tree. A reviewer will
happily report on a file that only exists inside a mutation harness.

Usage
-----
    python tools/review_fanout.py
    python tools/review_fanout.py --question "is the clear path idempotent?"
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / ".review"

DEFAULT_QUESTION = (
    "This diff changes live-money safety code. ONE question, max 20 lines, "
    "no summary: is there any path where trading resumes while a safety stop "
    "is active, or where success is reported but the change did not take "
    "effect? Answer YES/NO then specifics with file:line."
)

HERMES_SCRATCH = "~/hermes/cache/scratch/"


def export_diff() -> Path:
    """Write the working-tree diff as UTF-8. Never via shell redirection."""
    OUT_DIR.mkdir(exist_ok=True)
    target = OUT_DIR / "review.diff"

    tracked = subprocess.run(
        ["git", "diff", "HEAD"], cwd=REPO, capture_output=True, text=True
    ).stdout

    # Untracked test files are part of the change and must be reviewed too.
    extra = []
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=REPO,
        capture_output=True,
        text=True,
    ).stdout.split()
    for rel in untracked:
        if not rel.startswith("tests/") and not rel.endswith(".py"):
            continue
        d = subprocess.run(
            ["git", "diff", "--no-index", "--", "/dev/null", rel],
            cwd=REPO,
            capture_output=True,
            text=True,
        ).stdout
        if d:
            extra.append(f"=== NEW FILE: {rel} ===\n{d}")

    payload = tracked + "\n".join(extra)
    target.write_text(payload, encoding="utf-8")  # explicit, no BOM
    return target


def _run(name: str, cmd: list[str], cwd: Path | None = None, timeout: int = 900):
    try:
        p = subprocess.run(
            cmd,
            cwd=str(cwd or REPO),
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=isinstance(cmd, str),
        )
        return name, (p.stdout or "") + (p.stderr or ""), True
    except FileNotFoundError:
        return name, f"{name} is not installed / not on PATH", False
    except subprocess.TimeoutExpired:
        # A reviewer that timed out has NOT approved anything. Say so loudly.
        return name, f"TIMED OUT after {timeout}s -- NO VERDICT", False


def reviewers(diff: Path, question: str) -> dict:
    diff_s = str(diff)
    jobs = {
        "opencode": [
            "opencode",
            "run",
            f"{question} Read the attached diff file first.",
            "-f",
            diff_s,
        ],
        "claude": ["claude", "-p", f"{question} Repo: {REPO}. Diff: {diff_s}"],
        "dsh": ["dsh", "headless", f"{question} Diff at {diff_s}"],
        "hermes": ["hermes", "-z", f"{question} Diff at {diff_s}"],
        "ollama": [
            "ollama",
            "run",
            "qwen2.5-coder:7b",
            f"{question}\n\nThe diff is at {diff_s}. Read it first.",
        ],
    }
    results = {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [
            pool.submit(_run, name, cmd) for name, cmd in jobs.items()
        ]
        for f in futures:
            name, out, ok = f.result()
            results[name] = (out, ok)
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--question", default=DEFAULT_QUESTION)
    args = ap.parse_args()

    os.environ["PATH"] = os.environ.get("PATH", "")
    # hermes refuses the system temp dir.
    os.environ.setdefault("HOME", os.path.expanduser("~"))

    diff = export_diff()
    size = diff.stat().st_size
    print(f"[review] diff exported: {diff} ({size} bytes, UTF-8)")
    if size == 0:
        print("[review] nothing to review -- working tree is clean.")
        return 0

    print(f"[review] fanning out to 5 reviewers...\n")
    results = reviewers(diff, args.question)

    print("=" * 70)
    verdicts = []
    for name, (out, ok) in results.items():
        flag = "RETURNED" if ok else "NO VERDICT"
        print(f"\n--- {name}  [{flag}] ---")
        print(out.strip()[-1800:] if out.strip() else "(empty response)")
        if not ok:
            verdicts.append(f"{name}: no verdict")
        OUT_DIR.mkdir(exist_ok=True)
        (OUT_DIR / f"{name}.txt").write_text(out, encoding="utf-8")

    print("\n" + "=" * 70)
    counted = [n for n, (_, ok) in results.items() if ok]
    print(f"reviewers that returned: {len(counted)}/5  {counted}")
    if verdicts:
        print("NOT counted as approval: " + "; ".join(verdicts))
    print(f"full outputs: {OUT_DIR}")
    print(
        "\nReminder: check each verdict against the code before acting on it. "
        "A reviewer handed the answer in prose confirms your arithmetic, not "
        "reality."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())