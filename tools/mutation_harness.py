#!/usr/bin/env python3
"""
Mutation harness: break the product on purpose, confirm the tests notice.

A green test run proves nothing until you have seen that test go red. This is
the single highest-yield check in the repo -- on 2026-10-16 mutation testing
found two live-money guards that a full green suite was blind to.

Three bugs this harness itself had, all of which made it report false comfort:

1. Needles written with ``\\n`` against a CRLF file (16,091 CRLF lines here).
   Two of three mutations silently SKIPPED and the run still looked "fine".
2. Reverts verified with ``git diff --exit-code``, which compares to HEAD --
   so uncommitted work made every correct revert look dirty.
3. A SKIP counted as neither pass nor fail, so a mutation that never applied
   looked like a mutation that was handled.

So: line-ending-aware needles (CRLF and LF are both tried, because this
repo's worktree is mixed), SHA-256 revert verification against a
pre-mutation snapshot, and SKIP exits non-zero.

NEVER run this concurrently with a test run or a reviewer. Both will read a
file that is mid-mutation and report on code that does not exist.

Usage
-----
    python tools/mutation_harness.py tools/mutations/emergency_stop.json
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WSL_PY = "$HOME/dstest/bin/python"

# vitest colours its output, and the colour codes sit in front of the summary
# lines this harness reads.
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def load_spec(spec_path: Path) -> dict:
    return json.loads(spec_path.read_text(encoding="utf-8"))


def _wsl_path(p: Path) -> str:
    """Convert a Windows path into the path WSL can actually cd into.

    `C:/Users/x` is not a valid Linux path, so `cd C:/Users/x` fails inside
    WSL and pytest never runs. WSL mounts the C: drive at /mnt/c. A path
    that is already a Linux path is returned unchanged, so this is a no-op
    when the harness itself runs inside WSL.
    """
    s = str(p.resolve())
    if len(s) > 2 and s[1] == ":" and s[0].isalpha():
        return "/mnt/" + s[0].lower() + s[2:].replace("\\", "/")
    return s


def run_pytest(target: str, timeout: int = 1800) -> tuple[int, str]:
    """Run pytest under the grading-platform interpreter.

    Must NOT unconditionally shell out to `wsl`. When this harness is itself
    run from inside WSL (which is how it was invoked the first time), a nested
    `wsl` does not fail cleanly -- it resolves to some other binary and you get
    curl's help text where pytest's summary should be. Every mutant then reads
    as SURVIVED, and the run reports confident, total nonsense.
    """
    repo = _wsl_path(REPO)
    inner = (
        f"cd {repo} && export PATH=\"$HOME/dstest/bin:$PATH\" && "
        f"{WSL_PY} -m pytest {target} --tb=no -q"
    )
    if sys.platform.startswith("linux"):
        proc = subprocess.run(
            ["bash", "-lc", inner], capture_output=True, text=True, timeout=timeout
        )
    else:
        proc = subprocess.run(
            ["wsl", "bash", "-lc", inner], capture_output=True, text=True, timeout=timeout
        )

    out = (proc.stdout or "") + (proc.stderr or "")
    summary = ""
    for line in reversed(out.splitlines()):
        if " passed" in line or " failed" in line or " error" in line:
            summary = line.strip()
            break

    if not summary:
        # A missing summary means we did not actually run pytest. Reporting
        # this as "no failures" would be exactly the false green this harness
        # exists to prevent.
        return 3, "NO PYTEST SUMMARY -- pytest did not run. Output:\n" + out[-400:]
    return proc.returncode, summary


def run_command(argv: list[str], timeout: int = 1800) -> tuple[int, str]:
    """Run a test command that is not pytest (used by front-end specs).

    The harness was written for pytest, but a front-end change has no pytest
    target: its tests are run by vitest. A spec may therefore name the
    command that runs its tests, and a non-zero exit code means the tests
    failed -- which is exactly what "the mutant was caught" means.

    The command is run from the repository root. It is handed to the shell so
    that `npm` resolves to `npm.cmd` on Windows and to `npm` on Linux.
    """
    if os.name == "nt":
        line = subprocess.list2cmdline(argv)
    else:
        line = " ".join(shlex.quote(arg) for arg in argv)
    try:
        proc = subprocess.run(
            line,
            shell=True,
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 3, f"COMMAND DID NOT RUN: {exc}"

    out = (proc.stdout or "") + (proc.stderr or "")
    summary = ""
    for raw in reversed(out.splitlines()):
        stripped = ANSI_RE.sub("", raw).strip()
        if stripped.startswith("Test Files") or stripped.startswith("Tests "):
            summary = stripped
            break
    if not summary:
        # A missing summary means the command did not actually run the tests.
        # Reporting this as "no failures" is the false green this harness
        # exists to prevent.
        tail = [
            ANSI_RE.sub("", line).strip()
            for line in out.splitlines()
            if line.strip()
        ][-3:]
        return 3, "NO TEST SUMMARY -- the test command did not run. Output:\n" + "\n".join(tail)
    if summary_ran_no_test(summary):
        # Every test was SKIPPED. The command still exits 0, so this would
        # be recorded as "the mutant SURVIVED" -- a false green, and the
        # exact thing this harness exists to prevent. A run in which no
        # test ran is a BROKEN run, not a passing one.
        return 3, "ALL TESTS SKIPPED -- the test command ran no test: " + summary
    return proc.returncode, summary


def summary_ran_no_test(summary: str) -> bool:
    """Did the summary say that NO test actually ran?

    A vitest run in which every test is skipped still exits 0:

        Tests  139 skipped (139)

    That is not a pass. The harness would otherwise record the mutant as
    SURVIVED, which is a false green.

    A run that executed SOME tests is fine, however many were skipped:

        Tests  2 failed | 137 passed | 1 skipped (140)
    """
    if "skipped" not in summary:
        return False
    return "passed" not in summary


def eol_variants(text: str) -> list[bytes]:
    """The text in BOTH line-ending forms: CRLF first, then LF.

    The repo's worktree is MIXED: `bot_module/controller.py` and
    `bot_runner.py` are CRLF, while `tools/db_session_guard.py` and
    `bot_module/db_session.py` are LF. Git stores LF for both (`*
    text=auto` in .gitattributes), so the worktree's endings are whatever
    the file physically is. A needle written with `\n` must therefore be
    tried in BOTH forms, or the mutation silently SKIPs - and a skip is
    not a pass.
    """
    lf = text.replace("\r\n", "\n").encode("utf-8")
    return [lf.replace(b"\n", b"\r\n"), lf]


def apply(spec_path: Path) -> int:
    spec = load_spec(spec_path)
    test_target = spec.get("pytest_target", "tests/")
    # A spec may name a test command instead of a pytest target. The harness
    # was written for pytest; a front-end change has no pytest target, and
    # this keeps those specs on the same tool (same snapshot, same SHA-256
    # revert check, same SKIP-is-a-failure rule).
    command = spec.get("command")
    timeout = spec.get("timeout_s", 1800)

    # Snapshot every file the spec touches BEFORE anything is modified.
    files = sorted({REPO / m["file"] for m in spec["mutations"]})
    originals = {f: f.read_bytes() for f in files}
    digests = {f: hashlib.sha256(b).hexdigest() for f, b in originals.items()}

    print(f"spec: {spec_path.name}   pytest target: {test_target}")
    if command:
        print(f"  test command: {' '.join(command)}")
    for f in files:
        crlf_n = originals[f].count(b"\r\n")
        print(f"  snapshot {f.name}: {len(originals[f])} bytes, {crlf_n} CRLF")
    print()

    survivors: list[str] = []
    expected: list[str] = []
    skipped: list[str] = []

    try:
        for m in spec["mutations"]:
            label, rel = m["label"], REPO / m["file"]
            needle = repl = None
            for cand, cand_repl in zip(
                eol_variants(m["needle"]), eol_variants(m["replace"])
            ):
                if cand in originals[rel]:
                    needle, repl = cand, cand_repl
                    break

            if needle is None:
                skipped.append(label)
                print(f"SKIP     {label} -- needle NOT FOUND in {rel.name}")
                print("          (a skip is NOT a pass; fix the spec or the code)")
                continue

            rel.write_bytes(originals[rel].replace(needle, repl, 1))
            if command:
                rc, summary = run_command(command, timeout)
                # A non-zero exit code means the test command reported a
                # failure, which is what "the mutant was caught" means.
                killed = rc != 0
                broken = rc == 3
                broken_message = (
                    "ABORT: the test command did not produce a summary, so no "
                    "mutation result is trustworthy."
                )
            else:
                rc, summary = run_pytest(test_target, timeout)
                killed = ("failed" in summary) or ("error" in summary)
                broken = rc == 3
                broken_message = (
                    "ABORT: pytest did not produce a summary, so no mutation "
                    "result is trustworthy."
                )

            if broken:
                rel.write_bytes(originals[rel])
                print(f"BROKEN  {label}")
                print(f"          {summary}")
                print(broken_message, file=sys.stderr)
                return 2

            if not killed:
                if m.get("expected_survivor"):
                    expected.append(label)
                    tag = "SURVIVED (expected)"
                else:
                    survivors.append(label)
                    tag = "SURVIVED"
            else:
                tag = "KILLED  "

            print(f"{tag}  {label}")
            print(f"          -> {summary}")
            if not killed and m.get("expected_survivor"):
                print(f"          reason: {m.get('expected_survivor_reason', '')}")

            rel.write_bytes(originals[rel])
            ok = sha256(rel) == digests[rel]
            print(f"          revert verified by sha256: {'MATCH' if ok else '*** MISMATCH ***'}")
            print()

            if not ok:
                print("ABORT: tree is not in its pre-mutation state.", file=sys.stderr)
                return 2
    finally:
        for f, b in originals.items():
            f.write_bytes(b)

    print("=" * 66)
    for f in files:
        print(f"{f.name} restored: {sha256(f) == digests[f]}")
    print(f"skipped={len(skipped)}  survived={len(survivors)}  expected-survivors={len(expected)}")
    if expected:
        print("SURVIVED BY DESIGN (documented in the spec):")
        for e in expected:
            print(f"  - {e}")
    if survivors:
        print("SURVIVED (tests do NOT defend this -- real gap): " + "; ".join(survivors))
    if skipped:
        print("SKIPPED (spec drifted): " + "; ".join(skipped))

    if skipped or survivors:
        print("\nRESULT: FAIL -- an injected defect went unnoticed.")
        return 1
    print("\nRESULT: PASS -- every injected defect was caught by the tests.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(apply(Path(sys.argv[1]).resolve()))