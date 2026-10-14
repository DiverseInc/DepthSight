#!/usr/bin/env python3
"""
Make the CI gate a REGRESSION gate instead of a CLEANLINESS gate.

Problem this solves. Before this script, `pytest tests/` was the whole gate.
The repo carries long-standing failures, so the check was red on every single
commit -- including the ones that fixed real bugs. A badge that is always red is
indistinguishable from a badge that is always green: both get ignored.

The fix. Record the known-failing tests in tests/baseline_failures.json and
fail ONLY when a test outside that list breaks, or a known-failing test breaks in
MORE places than recorded. Then:

  * PR #3 (pandas aliases, 132 -> 20 failures)  -> GREEN, correctly
  * a PR that introduces a new failing test       -> RED, correctly
  * a PR that fixes a baseline failure           -> GREEN, and reported

WHY A COUNT AND NOT JUST A SET OF IDS. The baseline maps each canonical test id
to the MAXIMUM number of failing parametrised instances allowed for it. Two ids
below have 2 failing params; sixteen have 1. Collapsing a parametrized test to a
single id loses this: if `test_permission_enforcement` already has 2 failing
params in the baseline and a change makes a THIRD param fail, a pure set
comparison sees an id that was already present and reports GREEN on a real
regression. Counting per id catches it.

Ids are still the primary key -- a count on its own would let a new failure hide
behind a concurrently-fixed one.

Usage:
    python -m pytest tests/ --tb=no -q -rf > pytest_output.txt 2>&1 || true
    python scripts/check_test_baseline.py pytest_output.txt

Exit codes: 0 = no new failures, 1 = new failures (or the output is unreadable).
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILE = REPO_ROOT / "tests" / "baseline_failures.json"

# pytest prints, per failure, in the -rf short summary:
#   FAILED tests/test_api.py::test_get_status - AssertionError: ...
#   FAILED tests/test_plans.py::TestUserPlans::test_permission_enforcement[x] - ...
#
# GitHub prefixes every log line with "test\tRun tests\t<timestamp>\t", so the
# marker is NOT at the start of the line. Anchor on the token, not on ^.
#
# The node id is captured in FULL, including any "[parametrisation]" suffix, so
# distinct params count separately. `\S+` stops at the whitespace before the
# optional " - AssertionError..." detail, so that detail is not captured.
#
# The lookbehind stops this matching inside a longer word ("XFAILED",
# "NOTFAILED"). Requiring "tests/" after the whitespace keeps it off traceback
# prose that merely contains the word FAILED.
FAILED_RE = re.compile(r"(?<![A-Za-z0-9_])FAILED\s+(tests/\S+)")

# Collection/import failures are reported as ERROR, not FAILED. They must also
# break the gate, otherwise a broken import would show up as "no failures".
ERROR_RE = re.compile(r"(?<![A-Za-z0-9_])ERROR\s+(tests/\S+)")

# A pytest run that produced a real summary ends with something like:
#   "==== 20 failed, 1345 passed, 45 skipped in 14m50s ===="
#   "=== 1 failed, 4 passed in 0.5s ===="
#   "=== no tests ran in 0.1s ===="
SUMMARY_RE = re.compile(
    r"=+\s*(?:no tests ran|\d+\s+(?:failed|passed|error|errors|skipped))", re.IGNORECASE
)

# pytest's final epilogue states how many tests failed, e.g.
#   "===== 20 failed, 1345 passed, 45 skipped, 2 warnings in 14m50s ====="
# It is the ONLY place the run states its own failure total, so it is the only
# thing that can catch a log whose per-test FAILED lines never reached disk.
# Only the tail is searched -- a 15-minute run produces a large log, and the
# epilogue is always last.
FAILED_COUNT_RE = re.compile(r"(\d+)\s+failed\b", re.IGNORECASE)

# pytest's own catastrophic failures. Each is ANCHORED, not a bare substring:
# a bare "Interrupted" matches any test named test_interrupted_recovery and any
# log line mentioning it, which would report a fatal condition that never
# happened. Real pytest output is "!!!! Interrupted: N errors during collection !!!!".
FATAL_RES = (
    re.compile(r"(?m)^\s*INTERNALERROR\b"),
    re.compile(r"(?m)^\s*!+\s*:?\s*Interrupted\b"),
    re.compile(r"=+\s*no tests ran", re.IGNORECASE),
)

# ⚠️ XPASS is deliberately NOT treated as a failure. A strict-xfail test that
# unexpectedly passes IS a pytest failure, but this repo does not set
# `xfail_strict` (pytest.ini), so XPASS is reported as "xpassed" and does not
# affect the exit code or the failure total. Counting it unconditionally would
# turn a passing run red. Revisit only if xfail_strict is ever enabled.

# Parametrised ids appear once per parametrisation but share a function id
# until pytest appends [param]. Strip the bracket suffix so a baseline entry
# keeps matching when the parameter list changes shape.
PARAM_SUFFIX_RE = re.compile(r"\[.*\]$")


def parse_failures(output: str) -> Counter[str]:
    """Count failing test ids, per canonical id.

    Distinct LINES are counted, not raw occurrences: enabling more than one
    pytest reporter (e.g. `-rf -rA`) prints the same failure more than once and
    would inflate the count into a false regression. Two different params of the
    same function always differ in their node id, so they still count separately.
    """
    seen: set[str] = set()
    counts: Counter[str] = Counter()
    for match in FAILED_RE.finditer(output):
        if match.group(0) in seen:
            continue
        seen.add(match.group(0))
        counts[canonical(match.group(1))] += 1
    return counts


def parse_errors(output: str) -> set[str]:
    """Collection/import errors -- these break the gate too.

    A test that fails to IMPORT raises at collection time and is reported as
    `ERROR tests/...` or `ERROR collecting tests/...`, not `FAILED tests/...`.
    Ignoring those would mean a broken import shows up as zero failures, i.e.
    green.
    """
    return {canonical(m.group(1)) for m in ERROR_RE.finditer(output)}


def reported_failure_count(output: str) -> int | None:
    """The failure total pytest stated for itself, or None if unreadable.

    This exists to close a real hole. Every other check in this file asks "does
    the log LOOK complete?". This one asks the only question that actually
    matters: does the log AGREE WITH ITSELF? A log whose epilogue says
    "20 failed" while zero `FAILED tests/...` lines are present is a log that
    lost its failure lines -- and grading it as zero failures would report a
    green gate on a suite that broke. A partial flush or a post-processed log
    both produce exactly that shape.

    Only the tail is searched: the epilogue is last, and scanning a 15-minute
    log for the first "N failed" would pick up unrelated text.
    """
    match = FAILED_COUNT_RE.search(output[-4000:])
    return int(match.group(1)) if match else None


def output_is_trustworthy(output: str) -> tuple[bool, str]:
    """Refuse to grade output pytest never actually produced.

    The workflow runs pytest with `|| true`, so a crashed collection phase or a
    missing dependency exits 0 and prints no FAILED lines. Without this check,
    "no failures observed" would mean "nothing ran" and the gate would pass on
    nothing -- which is worse than being permanently red.
    """
    if not output.strip():
        return False, "pytest output is empty -- pytest produced nothing."
    if not SUMMARY_RE.search(output):
        return False, (
            "no pytest summary line found -- pytest crashed before reporting. "
            "A crashed run prints no FAILED lines, which would otherwise look "
            "like a clean pass."
        )
    for pattern in FATAL_RES:
        match = pattern.search(output)
        if match:
            return False, f"pytest reported a fatal condition: {match.group(0).strip()!r}"
    return True, ""


def canonical(test_id: str) -> str:
    """Drop the [parametrisation] suffix so the baseline entry stays stable."""
    return PARAM_SUFFIX_RE.sub("", test_id)


def load_baseline() -> tuple[dict[str, int], dict]:
    """Load and VALIDATE the baseline.

    The file is the one thing standing between "green" and "a regression I never
    saw", so it is checked rather than trusted. Every value must be a positive
    int, every key must look like a test id, and the per-id counts must add up to
    the recorded expected_failure_count. A baseline that contradicts itself is
    an authoring bug, and grading against it would produce a meaningless answer.
    """
    if not BASELINE_FILE.is_file():
        print(
            f"::error::baseline file missing: {BASELINE_FILE}\n"
            "Refusing to pass silently -- an unreadable baseline must not green the gate."
        )
        raise SystemExit(1)
    try:
        data = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"::error::baseline file is malformed: {exc}")
        raise SystemExit(1)

    # A JSON document can be a list, a string or a number. Without this check,
    # `"failures" not in data` is a legal membership test but `data["failures"]`
    # then raises TypeError -- a traceback instead of a graded decision. It
    # still exits non-zero, but a crash tells the reader nothing about why.
    if not isinstance(data, dict):
        print(
            "::error::baseline file must contain a JSON object at the top level, "
            f"got {type(data).__name__}"
        )
        raise SystemExit(1)

    if "failures" not in data:
        print("::error::baseline file has no 'failures' key")
        raise SystemExit(1)
    failures = data["failures"]
    if not isinstance(failures, dict):
        print(
            "::error::baseline 'failures' must be an object mapping test id -> "
            f"max failing count, got {type(failures).__name__}"
        )
        raise SystemExit(1)

    baseline: dict[str, int] = {}
    for test_id, allowed in failures.items():
        # bool is a subclass of int; `true` here is a mistake, not a count of 1.
        if not isinstance(allowed, int) or isinstance(allowed, bool) or allowed < 1:
            print(f"::error::baseline value for {test_id!r} must be an int >= 1, got {allowed!r}")
            raise SystemExit(1)
        canonical_id = canonical(test_id)
        if canonical_id != test_id:
            print(
                f"::error::baseline key {test_id!r} still carries a [parametrisation] "
                f"suffix. Store the canonical id {canonical_id!r} instead."
            )
            raise SystemExit(1)
        if "::" not in test_id:
            print(f"::error::baseline key {test_id!r} is not a pytest test node id")
            raise SystemExit(1)
        baseline[canonical_id] = allowed

    declared = data.get("expected_failure_count")
    if declared is None:
        print("::error::baseline file has no 'expected_failure_count'")
        raise SystemExit(1)
    actual = sum(baseline.values())
    if declared != actual:
        print(
            f"::error::baseline declares expected_failure_count={declared} but its "
            f"per-id counts add up to {actual}. Fix one or the other."
        )
        raise SystemExit(1)

    return baseline, data


def main(argv: list[str]) -> int:
    # CI is UTF-8, but this script also runs on developer machines whose
    # console is cp1252. Without this, printing any non-ASCII character raises
    # UnicodeEncodeError -- which would turn a GREEN gate into a crash. Keep
    # the markers ASCII regardless; this is belt-and-braces.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    if len(argv) != 2:
        print(f"usage: {Path(argv[0]).name} <pytest-output-file>")
        return 2

    output_file = Path(argv[1])
    if not output_file.is_file():
        print(
            f"::error::pytest output file not found: {output_file}\n"
            "If pytest died before producing output, that is a real failure."
        )
        return 1

    try:
        output = output_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        # is_file() does not check readability, so an unreadable file gets past
        # the check above and fails here.
        print(f"::error::could not read pytest output file {output_file}: {exc}")
        return 1

    # Never grade output pytest did not really produce. This is the difference
    # between "nothing broke" and "nothing ran".
    trustworthy, why = output_is_trustworthy(output)
    if not trustworthy:
        print(f"[FATAL] {why}")
        print("       Refusing to report a green gate on untrustworthy output.")
        print("       Tail of pytest_output.txt for context:")
        for line in output.strip().splitlines()[-30:]:
            print(f"       {line}")
        return 1

    observed = parse_failures(output)
    errored = parse_errors(output)
    baseline, _data = load_baseline()

    # Does the log agree with itself? pytest states its own failure total in the
    # epilogue; if that total exceeds the number of FAILED lines actually
    # present, the log lost its failure lines and grading it as a clean run
    # would be a green gate on a suite that broke.
    reported = reported_failure_count(output)
    observed_total = sum(observed.values())
    if reported is not None and reported > observed_total:
        print(
            "[FATAL] pytest reported "
            f"{reported} failure(s) but only {observed_total} FAILED line(s) "
            "were found in the log."
        )
        print(
            "       The log is inconsistent -- pytest's own epilogue disagrees "
            "with its\n       per-test output. Grading this would risk reporting a "
            "green gate on a\n       suite that actually broke. Tail of "
            "pytest_output.txt for context:"
        )
        for line in output.strip().splitlines()[-30:]:
            print(f"       {line}")
        return 1

    # A collection error is a NEW failure unless its id is already baselined.
    new_errors = sorted(errored - set(baseline))

    # A test is a regression if it fails MORE times than the baseline allows.
    # This is the check a pure set comparison cannot make.
    regressed: list[tuple[str, int, int]] = []
    for test_id, count in sorted(observed.items()):
        allowed = baseline.get(test_id, 0)
        if count > allowed:
            regressed.append((test_id, count, allowed))

    new_failures = sorted(
        {test_id for test_id, _c, _a in regressed if baseline.get(test_id, 0) == 0}
        | set(new_errors)
    )
    fixed = sorted(t for t in baseline if t not in observed and t not in errored)

    print("=" * 68)
    print(f"failing instances observed : {sum(observed.values())}")
    print(f"distinct failing tests     : {len(observed)}")
    print(f"baseline allows            : {sum(baseline.values())} "
          f"across {len(baseline)} test(s)")
    print("=" * 68)

    if regressed:
        print(f"\n[NEW FAILURE] {len(regressed)} test(s) outside what the baseline allows:")
        for test_id, count, allowed in regressed:
            if allowed == 0:
                print(f"   {test_id}  (failing {count}x, not in the baseline)")
            else:
                print(
                    f"   {test_id}  (failing {count}x, baseline allows {allowed}x "
                    f"-- {count - allowed} new failing parametrisation(s))"
                )

    if new_errors:
        print(f"\n[COLLECTION ERROR] {len(new_errors)} test(s) failed to collect/import:")
        for test_id in new_errors:
            print(f"   {test_id}")

    if new_failures or regressed or new_errors:
        print(
            "\nEither fix these, or -- if a failure is pre-existing and unrelated --\n"
            "confirm it also fails on `main` before considering a baseline entry.\n"
            "Never add an entry purely to turn the badge green; that hides regressions."
        )
        return 1

    if fixed:
        print(f"\n[FIXED] {len(fixed)} baseline failure(s) FIXED by this change:")
        for test_id in fixed:
            print(f"   {test_id}")
        print("\n   -> Remove these from tests/baseline_failures.json.")

    print(
        "\n[OK] No new failures. Every observed failure is a known, recorded one.\n"
        "   This badge is green because nothing regressed -- not because the\n"
        "   suite is clean. Known debt is tracked in tests/baseline_failures.json."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))