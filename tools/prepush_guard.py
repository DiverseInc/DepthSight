#!/usr/bin/env python3
"""
Pre-push guard: run the FULL suite and the real CI baseline gate before a push.

Why this exists
---------------
On 2026-10-16 the emergency-stop fix was pushed after running only the four
*adjacent* test files (63 passed). CI then failed on
``tests/test_achievements.py::test_pulling_the_plug_achievement`` -- a test in
a file that has nothing to do with controller.py but calls the same endpoint.

Running the adjacent files is not a weaker version of running the suite. It is
a different question, and it missed. The full suite takes ~22 minutes; finding
a 30-second fix is cheap by comparison.

This reuses ``scripts/check_test_baseline.py`` -- the same gate CI runs -- so
"green here" and "green on CI" mean the same thing.

Usage
-----
    python tools/prepush_guard.py                # full suite + baseline gate
    python tools/prepush_guard.py --quick        # targeted files only (NOT a
                                                 # substitute; for iteration)
    python tools/prepush_guard.py --install-hook # install as .git/hooks/pre-push

Exit codes: 0 green, 1 red.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# The WSL venv the grading platform runs in. CI is Linux; this box is Windows,
# so the suite has to run under WSL to match. See the handoff index for why the
# venv lives in $HOME and not /tmp.
WSL_VENV = "$HOME/dstest/bin/python"
WSL_REPO_MOUNT_PREFIX = "/mnt/c/"

# The suite takes ~22 min. Allow more, because a cold run on a loaded box is
# slower, and because a timeout that silently turns into "assume green" is
# exactly the failure mode this script exists to prevent.
FULL_SUITE_TIMEOUT_S = 45 * 60


def windows_repo_to_wsl(path: Path) -> str:
    """C:\\Users\\x -> /mnt/c/Users/x .

    WSL mounts Windows drives under /mnt/<drive>. The obvious-looking
    "/c/Users/..." is NOT valid and fails with 'No such file or directory',
    which is exactly how the first two versions of this guard failed while
    reporting a misleading "pytest output file not found".
    """
    s = str(path).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        return "/mnt/" + s[0].lower() + s[2:]
    return s


def in_wsl() -> bool:
    return sys.platform.startswith("linux")


def run_suite(target: str, timeout_s: int) -> tuple[int, str]:
    """Run pytest, write output to a file, then apply the CI baseline gate.

    No double quotes anywhere in the command string. Windows `subprocess`
    mangles embedded `"` when forwarding an argument into WSL, which is why
    the first version of this script silently failed to produce its output
    file and reported "output file not found". Redirect to a file instead of
    piping through `tee` so there is nothing to quote.
    """
    out_name = ".prepush_pytest_output.txt"
    # A Windows path like C:\Users\... is NOT a valid `cd` target in bash.
    # It must become /mnt/c/Users/... . Dropping this conversion is what made
    # the whole guard silently fail to produce its output file.
    repo = (
        str(REPO).replace("\\", "/")
        if in_wsl()
        else windows_repo_to_wsl(REPO)
    )
    inner = (
        f"cd {repo} && "
        f"{WSL_VENV} -m pytest {target} -q --tb=short > {out_name} 2>&1; "
        f"cat {out_name}; "
        f"{WSL_VENV} scripts/check_test_baseline.py {out_name}"
    )
    argv = ["bash", "-lc", inner] if in_wsl() else ["wsl", "bash", "-lc", inner]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def summarise(output: str) -> str:
    """Pull the lines that actually matter out of a long pytest+gate log."""
    keep = []
    for line in output.splitlines():
        if re.search(
            r"(passed|failed|error|ERROR|FAILED|NEW FAILURE|baseline allows|"
            r"failing instances|\[OK\]|short test summary)",
            line,
            re.IGNORECASE,
        ):
            keep.append(line.rstrip())
    return "\n".join(keep[-25:])


HOOK = """#!/bin/sh
# Installed by tools/prepush_guard.py --install-hook
exec python3 tools/prepush_guard.py
"""


def install_hook() -> int:
    hooks = REPO / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    target = hooks / "pre-push"
    if target.exists() and "prepush_guard" not in target.read_text(
        errors="ignore"
    ):
        print(f"refusing to overwrite existing hook: {target}", file=sys.stderr)
        return 1
    target.write_text(HOOK)
    os.chmod(target, 0o755)
    print(f"installed {target}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="targeted files only")
    ap.add_argument(
        "--install-hook", action="store_true", help="install as pre-push hook"
    )
    args = ap.parse_args()

    if args.install_hook:
        return install_hook()

    target = (
        "tests/test_emergency_stop_latch_persistence.py tests/test_achievements.py "
        "tests/test_safety_guard_regressions.py tests/test_risk_manager.py"
        if args.quick
        else "tests/"
    )
    timeout = 900 if args.quick else FULL_SUITE_TIMEOUT_S

    print(f"[prepush] target={target}  timeout={timeout}s")
    print("[prepush] running suite + CI baseline gate...\n")

    try:
        code, output = run_suite(target, timeout)
    except subprocess.TimeoutExpired:
        print(
            f"\n[prepush] TIMED OUT after {timeout}s.\n"
            "[prepush] REFUSING to treat a timeout as green. Investigate, then "
            "re-run.\n",
            file=sys.stderr,
        )
        return 1

    print("\n".join(summarise(output).splitlines()[-25:]))
    print()

    if code == 0:
        print("[prepush] GREEN - safe to push.")
        return 0

    print(
        "[prepush] RED - push blocked.\n"
        "[prepush] If a failure is genuinely pre-existing and unrelated, confirm "
        "it also fails on main before adding a baseline entry.\n"
        "[prepush] Do NOT re-add a baseline entry just to turn this green.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())