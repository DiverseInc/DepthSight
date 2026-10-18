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
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / ".review"

# How much diff text to paste inline for `ollama`. It runs a 7B model locally,
# so tokens are free; this only bounds prompt size and wall time.
OLLAMA_DIFF_CHARS = 12000

# MEASURED 2026-10-18 on a real diff whose correct answer was already known:
# 7b answered both questions CORRECTLY, and also answered both correctly with
# NO DIFF IN THE PROMPT AT ALL. Its accuracy therefore carries no information --
# it is indistinguishable from a coin flip, and on 2026-10-18 it returned a
# confident "I found no issues" about a diff that disabled a stop-loss.
# qwen2.5-coder:14b was tried as the upgrade: 4x slower (94s vs 23s) and got the
# central question WRONG. There is no trustworthy local reviewer at this VRAM
# budget, so keep the fast one for triage and never treat it as a verdict.
OLLAMA_MODEL = "qwen2.5-coder:7b"

# Reviewers whose output is triage, not a verdict. Printed with that label so a
# confident-but-ungrounded local answer cannot sit in a column that reads like
# an approval from a model that can actually see the diff.
ADVISORY_REVIEWERS = {"ollama"}

DEFAULT_QUESTION = (
    "This diff changes live-money safety code. ONE question, max 20 lines, "
    "no summary: is there any path where trading resumes while a safety stop "
    "is active, or where success is reported but the change did not take "
    "effect? Answer YES/NO then specifics with file:line."
)

HERMES_SCRATCH = "~/hermes/cache/scratch/"


def export_diff(base: str | None = None) -> Path:
    """Write the change under review as UTF-8. Never via shell redirection.

    Without `base` this reviews only the uncommitted working tree -- which
    silently reviews NOTHING the moment the work is committed, i.e. exactly
    when a pre-merge review matters most. Pass `--base origin/main` to review
    the committed branch AND any working-tree changes on top of it.
    """
    OUT_DIR.mkdir(exist_ok=True)
    target = OUT_DIR / "review.diff"

    chunks: list[str] = []
    if base:
        committed = subprocess.run(
            ["git", "diff", f"{base}...HEAD"],
            cwd=REPO,
            capture_output=True,
            text=True,
        ).stdout
        if committed:
            chunks.append(f"=== COMMITTED: {base}...HEAD ===\n{committed}")
    uncommitted = subprocess.run(
        ["git", "diff", "HEAD"], cwd=REPO, capture_output=True, text=True
    ).stdout
    if uncommitted:
        chunks.append(f"=== UNCOMMITTED: git diff HEAD ===\n{uncommitted}")
    tracked = "\n".join(chunks)

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


def _resolve_argv(cmd: list[str]) -> tuple[list[str], str] | None:
    """Make a bare tool name actually launchable on Windows.

    Windows CreateProcess does NOT resolve `.cmd`/`.bat` from a bare name --
    only `.exe`. So `subprocess.run(["dsh", ...])` raises FileNotFoundError and
    a naive handler reports "not installed / not on PATH" for a tool that is
    present and working. `shutil.which` finds `dsh.CMD` happily; the launch
    is what fails. So: resolve to an absolute path, then dispatch on kind.

      .exe                -> run directly
      .cmd / .bat         -> cmd /c <path>
      .ps1                -> powershell -NoProfile -File <path>

    Returns (argv, why) or None when the tool genuinely is absent.
    """
    exe = cmd[0]
    path = shutil.which(exe)
    if path is None:
        for ext in (".cmd", ".exe", ".ps1", ".bat"):
            path = shutil.which(exe + ext)
            if path:
                break
    if path is None:
        return None

    low = path.lower()
    if low.endswith(".ps1"):
        return (
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                path,
            ]
            + cmd[1:],
            "powershell -File",
        )
    if low.endswith((".cmd", ".bat")):
        return ["cmd", "/c", path] + cmd[1:], "cmd /c"
    return [path] + cmd[1:], "direct"


_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")


def _meaningful(text: str) -> str:
    """Strip terminal control codes and judge whether there is a real answer.

    `ollama run` paints a braille spinner to the same stream it writes the
    answer to. Non-empty stdout therefore proves NOTHING -- the first working
    run scored 5/5 with three of them returning pure spinner glyphs. Requiring
    actual words is the whole point: a reviewer that produced no prose has not
    approved anything.
    """
    cleaned = _ANSI.sub("", text or "")
    # Drop the spinner glyphs themselves (U+2800 braille block).
    cleaned = "".join(ch for ch in cleaned if not (0x2800 <= ord(ch) <= 0x28FF))
    return cleaned.strip()


def _run(name: str, cmd: list[str], cwd: Path | None = None, timeout: int = 900):
    resolved = _resolve_argv(cmd)
    if resolved is None:
        return name, f"{name} is genuinely not installed / not on PATH", False
    argv, how = resolved
    # This box exports OLLAMA_HOST=0.0.0.0:11434. Ollama answers on
    # 127.0.0.1; the wildcard address makes the CLI drop connections under
    # parallel load and every ollama review came back `EOF`. Pin it per
    # subprocess rather than trusting the ambient environment.
    env = dict(os.environ)
    if "OLLAMA_HOST" in env or name == "ollama":
        env["OLLAMA_HOST"] = "http://127.0.0.1:11434"
    if name == "hermes":
        # `hermes -z` is the only working form; there is no `hermes run`. It
        # refuses to read the system temp dir and wants a scratch dir under the
        # user's own tree, so give it one that exists rather than letting it
        # fail and be scored as "no verdict".
        home = Path(env.get("USERPROFILE") or env.get("HOME") or Path.home())
        scratch = home / "hermes" / "cache" / "scratch"
        try:
            scratch.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
    try:
        p = subprocess.run(
            argv,
            cwd=str(cwd or REPO),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
        raw = (p.stdout or "") + (p.stderr or "")
        out = _meaningful(raw)
        # Require a real sentence, not just any character.
        words = len(re.findall(r"[A-Za-z0-9]{2,}", out))
        if words < 5:
            return name, (
                f"no usable verdict (exit {p.returncode}, {words} words after "
                f"stripping control codes) -- NO VERDICT"
            ), False
        return name, out, True
    except FileNotFoundError:
        return name, (
            f"launch failed (FileNotFoundError) via {how}; the binary exists at "
            f"{argv[0] if argv else '?'} but could not be started"
        ), False
    except subprocess.TimeoutExpired:
        # A reviewer that timed out has NOT approved anything.
        return name, f"TIMED OUT after {timeout}s -- NO VERDICT", False
    except Exception as exc:
        return name, f"{name} runner crashed: {type(exc).__name__}: {exc}", False


def reviewers(diff: Path, question: str, include_paid: bool = False) -> dict:
    """Build the reviewer set.

    Cost matters here. The DEFAULT set is the free/local tier only:

      ollama  - local model, zero tokens, zero subscription
      hermes  - local, zero tokens, zero subscription
      dsh     - no cloud key configured on this box

    Claude Code and Codex are third-party subscriptions. They are genuinely
    good reviewers -- but each call spends money that does not come from the
    plan this work is already paid for. They are opt-in behind --with-paid so
    that a routine safety review never quietly bills anything.

    `opencode` is included by default because it resolves without a cloud key
    here; if it starts reporting auth errors, move it behind the flag.
    """
    diff_s = str(diff)
    # `ollama run` has NO file-reading tool. Handed only a path -- which is what
    # this did until 2026-10-18 -- it answers from an empty context, which is
    # worse than no reviewer at all: it produces a confident, grounded-sounding
    # verdict about code it never saw. It returned "I found no issues with..."
    # for a diff that contained a stop-loss-disabling bug. Ollama is local and
    # free, so inlining the diff costs nothing but text.
    diff_text = diff.read_text(encoding="utf-8", errors="replace")
    if len(diff_text) > OLLAMA_DIFF_CHARS:
        diff_text = (
            diff_text[:OLLAMA_DIFF_CHARS]
            + f"\n\n... [TRUNCATED: showing the first "
            f"{OLLAMA_DIFF_CHARS} of {len(diff_text)} characters] ...\n"
        )

    jobs = {
        "ollama": [
            "ollama",
            "run",
            OLLAMA_MODEL,
            f"{question}\n\n"
            f"Review THIS diff, pasted inline. It is the complete diff:\n\n"
            f"```diff\n{diff_text}\n```\n\n"
            f"Answer only from the diff above. If it does not contain enough "
            f"to answer, say exactly that.",
        ],
        "hermes": [
            "hermes",
            "-z",
            f"{question}\n\n"
            f"Review THIS diff, pasted inline. It is the complete diff:\n\n"
            f"```diff\n{diff_text}\n```\n\n"
            f"Answer only from the diff above.",
        ],
        "dsh": ["dsh", "headless", f"{question} Diff at {diff_s}"],
        "opencode": [
            "opencode",
            "run",
            f"{question} Read the attached diff file first.",
            "-f",
            diff_s,
        ],
    }
    if include_paid:
        jobs["claude"] = [
            "claude",
            "-p",
            f"{question} Repo: {REPO}. Diff: {diff_s}",
        ]
    return jobs


def run_all(jobs: dict) -> dict:
    results = {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [pool.submit(_run, name, cmd) for name, cmd in jobs.items()]
        for f in futures:
            name, out, ok = f.result()
            results[name] = (out, ok)
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--question", default=DEFAULT_QUESTION)
    ap.add_argument(
        "--base",
        default=None,
        help="git ref to review against, e.g. origin/main. Reviews the committed "
        "branch plus working-tree changes. Without it, only uncommitted work is "
        "reviewed -- which is empty right after a commit.",
    )
    ap.add_argument(
        "--with-paid",
        action="store_true",
        help="also run Claude Code (separate Anthropic subscription). Off by "
        "default so a routine review never spends another plan's budget.",
    )
    args = ap.parse_args()

    # Reviewers emit UTF-8 (Ollama alone produces braille spinner glyphs).
    # The Windows console defaults to cp1252 and raises UnicodeEncodeError on
    # print -- which killed the fan-out AFTER the reviews had run. Reconfigure
    # stdout/stderr so a successful review cannot crash the reporter.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    os.environ["PATH"] = os.environ.get("PATH", "")
    # hermes refuses the system temp dir.
    os.environ.setdefault("HOME", os.path.expanduser("~"))

    diff = export_diff(args.base)
    size = diff.stat().st_size
    print(f"[review] diff exported: {diff} ({size} bytes, UTF-8)")
    if size == 0:
        print("[review] nothing to review -- working tree is clean.")
        return 0

    jobs = reviewers(diff, args.question, include_paid=args.with_paid)
    tier = (
        "FREE (local / no cloud key)"
        if not args.with_paid
        else "INCLUDING PAID SUBSCRIPTION REVIEWERS"
    )
    print(f"[review] reviewer tier: {tier}")
    print(f"[review] fanning out to {len(jobs)}: {', '.join(jobs)}\n")
    results = run_all(jobs)

    print("=" * 70)
    verdicts = []
    for name, (out, ok) in results.items():
        flag = "RETURNED" if ok else "NO VERDICT"
        if name in ADVISORY_REVIEWERS:
            flag += " / TRIAGE ONLY - NOT A VERDICT"
        print(f"\n--- {name}  [{flag}] ---")
        print(out.strip()[-1800:] if out.strip() else "(empty response)")
        if not ok:
            verdicts.append(f"{name}: no verdict")
        elif name in ADVISORY_REVIEWERS:
            verdicts.append(f"{name}: triage only, not counted as a verdict")
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
    print(
        "\nTRIAGE ONLY (see ADVISORY_REVIEWERS): the local ollama model was "
        "measured scoring CORRECTLY on a diff and CORRECTLY with no diff at "
        "all. Its accuracy is indistinguishable from a coin flip, so it is "
        "reported but never counted as approval. A reviewer that agrees with "
        "you about a diff it cannot see is worse than no reviewer."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())