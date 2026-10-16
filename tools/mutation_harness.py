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

So: CRLF-aware needles, SHA-256 revert verification against a pre-mutation
snapshot, and SKIP exits non-zero.

NEVER run this concurrently with a test run or a reviewer. Both will read a
file that is mid-mutation and report on code that does not exist.

Usage
-----
    python tools/mutation_harness.py tools/mutations/emergency_stop.json
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WSL_PY = "$HOME/dstest/bin/python"


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def load_spec(spec_path: Path) -> dict:
    return json.loads(spec_path.read_text(encoding="utf-8"))


def run_pytest(target: str, timeout: int = 1800) -> tuple[int, str]:
    """Run pytest under the grading-platform interpreter.

    Must NOT unconditionally shell out to `wsl`. When this harness is itself
    run from inside WSL (which is how it was invoked the first time), a nested
    `wsl` does not fail cleanly -- it resolves to some other binary and you get
    curl's help text where pytest's summary should be. Every mutant then reads
    as SURVIVED, and the run reports confident, total nonsense.
    """
    repo = str(REPO).replace("\\", "/")
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


def crlf(text: str) -> bytes:
    """Match the file's actual line ending instead of assuming LF."""
    return text.replace("\n", "\r\n").encode("utf-8")


def apply(spec_path: Path) -> int:
    spec = load_spec(spec_path)
    test_target = spec.get("pytest_target", "tests/")
    timeout = spec.get("timeout_s", 1800)

    # Snapshot every file the spec touches BEFORE anything is modified.
    files = sorted({REPO / m["file"] for m in spec["mutations"]})
    originals = {f: f.read_bytes() for f in files}
    digests = {f: hashlib.sha256(b).hexdigest() for f, b in originals.items()}

    print(f"spec: {spec_path.name}   pytest target: {test_target}")
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
            needle, repl = crlf(m["needle"]), crlf(m["replace"])

            if needle not in originals[rel]:
                skipped.append(label)
                print(f"SKIP     {label} -- needle NOT FOUND in {rel.name}")
                print("          (a skip is NOT a pass; fix the spec or the code)")
                continue

            rel.write_bytes(originals[rel].replace(needle, repl, 1))
            rc, summary = run_pytest(test_target, timeout)

            if rc == 3:
                rel.write_bytes(originals[rel])
                print(f"BROKEN  {label}")
                print(f"          {summary}")
                print(
                    "ABORT: pytest did not produce a summary, so no mutation "
                    "result is trustworthy.",
                    file=sys.stderr,
                )
                return 2

            killed = ("failed" in summary) or ("error" in summary)
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