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
4. The ``command`` path decided "the mutant was caught" from the test
   command's EXIT CODE. A collection error, an import failure, a timeout or
   a bad command line all exit non-zero without a test having failed, so
   every one of them was recorded as a false KILL -- the exact failure this
   harness exists to prevent. The test runner's own SUMMARY is the only
   authority on "did a test fail", and it is now what both paths use.
5. Two harness runs on the same tree at the same time each saw the OTHER
   one's mutation, and each still reported "revert verified by sha256: MATCH"
   -- because each run restores its own snapshot. The file is now checked
   against the snapshot BEFORE every mutation, and a file that changed under
   the run aborts it instead of producing a confident wrong answer.
6. The timeout did not kill the PROCESS TREE. `npm` -> `node` -> `vitest`
   leaves a grandchild holding the output pipe, so on Windows the timeout
   killed only the direct child and the harness then blocked forever on the
   pipe: a HUNG HARNESS, measured sitting on one mutant for 13 minutes past
   its own timeout. The output now goes to a file (which no grandchild holds
   open) and the whole tree is killed, so a timeout is reported as
   INCONCLUSIVE instead of hanging or being called a kill.
7. The `command` key let a spec name ANY program as its test runner, and the
   verdict was that program's summary and nothing else: a command that
   prints a failing summary without running a single test was recorded as a
   KILL, and the run reported "RESULT: PASS -- every injected defect was
   caught by the tests". A spec may now ask for a CLEAN-TREE BASELINE
   ("clean_tree_must_pass": true): the command is run once before any
   mutation, and a failure there stops the run, because a command that
   already fails on the clean tree cannot tell the mutant from the tree it
   started from. The same trust is the pytest path's: a spec whose pytest
   target has a KNOWN failure on the clean tree reports every mutant as
   caught, so the same key applies there.

So: line-ending-aware needles (CRLF and LF are both tried, because this
repo's worktree is mixed), SHA-256 revert verification against a
pre-mutation snapshot, a pre-mutation check that the tree was not touched
by anyone else, an optional clean-tree baseline that a spec can ask the
harness to enforce, SKIP exits non-zero, and a result that cannot be
determined is INCONCLUSIVE -- never a kill.

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
import signal
import subprocess
import sys
import tempfile
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


def summary_reports_failure(summary: str) -> bool:
    """True when the test runner's own summary says a test FAILED or ERRORED.

    The EXIT CODE is deliberately NOT consulted. A collection error, an
    import failure, a timeout or a bad command line all exit non-zero
    without a test having failed, and recording those as KILLED is a false
    kill -- the exact failure this harness exists to prevent.

    Both runners print the same shape of summary line, so both paths call
    this one function and cannot drift apart again.
    """
    return ("failed" in summary) or ("error" in summary)


def vitest_summary(out: str) -> str:
    """The vitest summary, as ONE line.

    vitest prints TWO summary lines: "Test Files  1 failed (1)" and
    "Tests  1 failed | 26 passed (27)". BOTH are taken, because the
    "Test Files" line is the one that reports a file which could not even
    be loaded -- and pytest reports that as "1 error", which the pytest
    path counts as a kill. Taking only the "Tests" line would silently
    drop it, and the two paths would not agree.
    """
    files_line = tests_line = ""
    for raw in out.splitlines():
        stripped = ANSI_RE.sub("", raw).strip()
        if stripped.startswith("Test Files"):
            files_line = stripped
        elif stripped.startswith("Tests "):
            tests_line = stripped
    return " | ".join(line for line in (files_line, tests_line) if line)


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill a command AND everything it started."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=60,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_captured(argv, cwd: Path, timeout: int) -> tuple[int, str, bool]:
    """Run a command, capturing its combined output. Returns (rc, out, timed_out).

    The output goes to a FILE, not to a pipe. A pipe is inherited by every
    grandchild the command leaves behind -- `npm` -> `node` -> `vitest` --
    so on Windows a timeout that killed only the direct child would block
    here forever, waiting for a pipe that the surviving grandchild still
    holds open. That is not a slow test; it is a HUNG HARNESS, and it was
    measured happening (the harness sat on one mutant for 13 minutes past
    its own timeout).

    A file is not held open that way, and the whole process TREE is killed
    when the timeout expires, so the timeout is honoured and the mutant is
    reported as INCONCLUSIVE -- never as a kill.
    """
    handle = tempfile.NamedTemporaryFile(
        mode="w+", encoding="utf-8", errors="replace", suffix=".log", delete=False
    )
    log_path = handle.name
    handle.close()
    timed_out = False
    try:
        with open(log_path, "w", encoding="utf-8", errors="replace") as fh:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd),
                stdout=fh,
                stderr=subprocess.STDOUT,
                shell=isinstance(argv, str),
                start_new_session=(os.name != "nt"),
            )
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_tree(proc)
                try:
                    rc = proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    rc = -1
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            out = fh.read()
    except OSError as exc:
        return 3, f"COMMAND DID NOT RUN: {exc}", False
    finally:
        try:
            os.unlink(log_path)
        except OSError:
            pass
    return rc, out, timed_out


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
        argv = ["bash", "-lc", inner]
    else:
        argv = ["wsl", "bash", "-lc", inner]

    rc, out, timed_out = _run_captured(argv, REPO, timeout)
    if timed_out:
        return 3, (
            f"NO PYTEST SUMMARY -- pytest did not finish within {timeout}s, so "
            f"there is no verdict. Output tail:\n{out[-400:]}"
        )

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
    return rc, summary


def run_command(argv: list[str], timeout: int = 1800) -> tuple[int, str]:
    """Run a test command that is not pytest (used by front-end specs).

    The harness was written for pytest, but a front-end change has no pytest
    target: its tests are run by vitest. A spec may therefore name the
    command that runs its tests.

    The command is run from the repository root. It is handed to the shell so
    that `npm` resolves to `npm.cmd` on Windows and to `npm` on Linux.

    The returned exit code is NOT the verdict on "was the mutant caught" --
    the caller decides that from the returned summary, exactly as it does
    for pytest. A non-zero exit on its own means nothing: a collection
    error, an import failure, a timeout or a bad command line all exit
    non-zero without a test having failed.
    """
    if os.name == "nt":
        line = subprocess.list2cmdline(argv)
    else:
        line = " ".join(shlex.quote(arg) for arg in argv)
    rc, out, timed_out = _run_captured(line, REPO, timeout)
    if timed_out:
        return 3, (
            f"NO TEST SUMMARY -- the test command did not finish within "
            f"{timeout}s, so there is no verdict. Output tail:\n{out[-400:]}"
        )

    summary = vitest_summary(out)
    if not summary:
        # A missing summary means the command did not actually run the tests.
        # Reporting this as "no failures" is the false green this harness
        # exists to prevent -- and it must never be reported as a kill.
        tail = [
            ANSI_RE.sub("", line).strip()
            for line in out.splitlines()
            if line.strip()
        ][-3:]
        return 3, "NO TEST SUMMARY -- the test command did not run. Output:\n" + "\n".join(tail)
    return rc, summary


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
    # A spec that injects NOTHING has nothing to catch, so a run of it would
    # report "every injected defect was caught by the tests" about ZERO
    # defects -- a green proof of nothing. That is the one way this harness
    # can be made to say PASS without having been asked a question, so it is
    # refused outright.
    if not spec.get("mutations"):
        print("SPEC ERROR  this spec injects NO mutations: there is nothing to catch,")
        print("            so a run of it would report a PASS that proves nothing.")
        print("\nRESULT: FAIL -- the spec injects no defect.")
        return 1
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

    # A kill means "the tests caught THIS mutation". That needs the tests to
    # PASS on the clean tree: a command that already fails there cannot tell
    # the mutant from the tree it started from, so every kill it reports
    # would be a false kill -- and the run would report a PASS.
    #
    # A spec that asks for the check ("clean_tree_must_pass") gets it: the
    # command is run once on the clean tree, and a failure there stops the
    # run before any mutant is recorded. A spec that does not ask for it is
    # trusted, and says so -- the trust is visible in the output instead of
    # being silent.
    if spec.get("clean_tree_must_pass"):
        if command:
            rc, baseline = run_command(command, timeout)
        else:
            rc, baseline = run_pytest(test_target, timeout)
        print(f"clean-tree baseline: {baseline}")
        if summary_reports_failure(baseline):
            print("SPEC ERROR  the test command reports a failure on the CLEAN tree,")
            print("            so no kill can be attributed to the mutation.")
            print("\nRESULT: FAIL -- the test command cannot tell the mutant from the clean tree.")
            return 1
        print()
    elif command:
        print("NOTE: this spec trusts its test command's summary WITHOUT a clean-tree baseline:")
        print("      a command that already fails on the clean tree cannot be told apart")
        print('      from one that caught the mutant. Set "clean_tree_must_pass" to true')
        print("      to make the harness check it before it records any kill.")
        print()

    survivors: list[str] = []
    expected: list[str] = []
    skipped: list[str] = []
    inconclusive: list[str] = []

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

            # Somebody else may be mutating this tree at the same time -- a
            # second harness run, or an editor. Then the verdict below would
            # be about THEIR mutation, not this one, and the SHA-256 revert
            # check would still say MATCH, because each run restores its own
            # snapshot. So the file is checked BEFORE the mutation: it must
            # still be exactly what was snapshotted.
            if sha256(rel) != digests[rel]:
                print(
                    f"ABORT     {label} -- {rel.name} changed under this run.",
                    file=sys.stderr,
                )
                print(
                    "          Another process is mutating this tree (a second "
                    "harness run?), so no verdict here would be trustworthy.",
                    file=sys.stderr,
                )
                return 2

            rel.write_bytes(originals[rel].replace(needle, repl, 1))
            if command:
                rc, summary = run_command(command, timeout)
            else:
                rc, summary = run_pytest(test_target, timeout)

            # The EXIT CODE is not the authority on "was the mutant caught".
            # The test runner's own SUMMARY is. A collection error, an import
            # failure, a timeout or a bad command line all exit non-zero
            # without a test having failed, and recording those as KILLED is
            # the false kill this harness exists to prevent.
            if rc == 3:
                # No summary could be parsed, so the runner reported no
                # verdict at all. The result is INCONCLUSIVE: never a kill,
                # and never "the tests missed it" either.
                rel.write_bytes(originals[rel])
                inconclusive.append(label)
                print(f"INCONCLUSIVE  {label}")
                print(f"          -> {summary}")
                print(
                    "          the test runner produced no summary, so this "
                    "mutation has NO verdict (it is never a kill)",
                    file=sys.stderr,
                )
                ok = sha256(rel) == digests[rel]
                print(
                    "          revert verified by sha256: "
                    f"{'MATCH' if ok else '*** MISMATCH ***'}"
                )
                print()
                if not ok:
                    print(
                        "ABORT: tree is not in its pre-mutation state.",
                        file=sys.stderr,
                    )
                    return 2
                continue

            killed = summary_reports_failure(summary)
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
    print(f"skipped={len(skipped)}  survived={len(survivors)}  expected-survivors={len(expected)}  inconclusive={len(inconclusive)}")
    if expected:
        print("SURVIVED BY DESIGN (documented in the spec):")
        for e in expected:
            print(f"  - {e}")
    if survivors:
        print("SURVIVED (tests do NOT defend this -- real gap): " + "; ".join(survivors))
    if skipped:
        print("SKIPPED (spec drifted): " + "; ".join(skipped))
    if inconclusive:
        print(
            "INCONCLUSIVE (no verdict -- never a kill): " + "; ".join(inconclusive)
        )

    if skipped or survivors or inconclusive:
        print("\nRESULT: FAIL -- an injected defect went unnoticed.")
        if inconclusive:
            print(
                "          (and/or a mutation result was INCONCLUSIVE, which is "
                "never a kill)"
            )
        return 1
    print("\nRESULT: PASS -- every injected defect was caught by the tests.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(apply(Path(sys.argv[1]).resolve()))