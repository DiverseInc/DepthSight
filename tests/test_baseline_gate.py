"""
Regression tests for the CI baseline gate (scripts/check_test_baseline.py).

The gate is the single thing standing between a green badge and a regression
nobody noticed. If it silently stops catching failures it is worse than having
no gate at all, so its behaviour is pinned here.

These tests deliberately do NOT read the real tests/baseline_failures.json for
their pass/fail expectations -- that file is expected to shrink over time as
bugs get fixed, and tests that break when someone does maintenance are a bad
neighbour. The gate is graded against temporary baselines written by the test.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_test_baseline.py"


def _load_gate():
    """Import the gate by path; it lives in scripts/, not an importable package."""
    spec = importlib.util.spec_from_file_location("check_test_baseline", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_test_baseline"] = module
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


def summary(passed: int = 100, failed: int = 1, skipped: int = 0) -> str:
    """A pytest short-summary block, shaped like the real thing."""
    return f"===== {failed} failed, {passed} passed, {skipped} skipped in 3.2s =====\n"


def failed_line(test_id: str) -> str:
    """One line of pytest's -rf output, as GitHub Actions prefixes it in logs."""
    return f"test\tRun tests\t2026-10-14T12:00:00Z\tFAILED {test_id} - AssertionError\n"


@pytest.fixture
def baseline_file(tmp_path, monkeypatch):
    """Redirect the gate at a throwaway baseline file and return a setter."""
    path = tmp_path / "baseline_failures.json"
    monkeypatch.setattr(gate, "BASELINE_FILE", path)

    def write(failures: dict[str, int], expected: int | None = None) -> Path:
        path.write_text(
            json.dumps(
                {
                    "recorded_at": "test",
                    "expected_failure_count": (
                        sum(failures.values()) if expected is None else expected
                    ),
                    "failures": failures,
                }
            ),
            encoding="utf-8",
        )
        return path

    return write


def run_gate(tmp_path, baseline_file, failures, output: str, expected: int) -> None:
    """Write a pytest log, run the gate over it, assert the exit code."""
    baseline_file(failures)
    log = tmp_path / "pytest_output.txt"
    log.write_text(output, encoding="utf-8")
    assert gate.main(["check_test_baseline.py", str(log)]) == expected


# --- the happy path -------------------------------------------------------


def test_baseline_matches_observed_is_green(tmp_path, baseline_file):
    run_gate(
        tmp_path,
        baseline_file,
        {"tests/test_a.py::test_one": 1},
        failed_line("tests/test_a.py::test_one") + summary(failed=1),
        expected=0,
    )


def test_fewer_failures_than_baseline_is_green(tmp_path, baseline_file):
    """Fixing a baseline failure must not turn CI red -- that is the whole point."""
    run_gate(
        tmp_path,
        baseline_file,
        {"tests/test_a.py::test_one": 1, "tests/test_b.py::test_two": 1},
        failed_line("tests/test_a.py::test_one") + summary(failed=1),
        expected=0,
    )


def test_reduced_baseline_is_reported_as_fixed(tmp_path, baseline_file, capsys):
    run_gate(
        tmp_path,
        baseline_file,
        {"tests/test_a.py::test_one": 1, "tests/test_b.py::test_two": 1},
        failed_line("tests/test_a.py::test_one") + summary(failed=1),
        expected=0,
    )
    out = capsys.readouterr().out  # read once -- a second read returns empty
    assert "FIXED" in out
    assert "tests/test_b.py::test_two" in out


# --- new failures are caught ---------------------------------------------


def test_unknown_test_failing_is_red(tmp_path, baseline_file):
    run_gate(
        tmp_path,
        baseline_file,
        {"tests/test_a.py::test_one": 1},
        failed_line("tests/test_a.py::test_one")
        + failed_line("tests/test_new.py::test_brand_new")
        + summary(failed=2),
        expected=1,
    )


def test_known_test_failing_more_often_than_allowed_is_red(tmp_path, baseline_file):
    """The regression a pure set comparison cannot see.

    `test_thing` has 2 failing params in the baseline. A change breaks a THIRD
    param. The id was already in the baseline, so an id-set comparison sees
    nothing new and reports GREEN on a real regression.
    """
    run_gate(
        tmp_path,
        baseline_file,
        {"tests/test_a.py::TestThing::test_thing": 2},
        failed_line("tests/test_a.py::TestThing::test_thing[p1]")
        + failed_line("tests/test_a.py::TestThing::test_thing[p2]")
        + failed_line("tests/test_a.py::TestThing::test_thing[p3]")  # <- new
        + summary(failed=3),
        expected=1,
    )


def test_new_param_of_an_already_failing_single_param_test_is_red(tmp_path, baseline_file):
    """Same hole, narrower case: baseline allows 1, two params now fail."""
    run_gate(
        tmp_path,
        baseline_file,
        {"tests/test_a.py::test_thing": 1},
        failed_line("tests/test_a.py::test_thing[p1]")
        + failed_line("tests/test_a.py::test_thing[p2]")
        + summary(failed=2),
        expected=1,
    )


def test_collection_error_is_red(tmp_path, baseline_file):
    """A broken import is not a failure -- but it must not read as a clean pass."""
    run_gate(
        tmp_path,
        baseline_file,
        {"tests/test_a.py::test_one": 1},
        "ERROR tests/test_broken.py\n" + summary(failed=0),
        expected=1,
    )


# --- pytest never actually ran -------------------------------------------


def test_empty_output_is_red(tmp_path, baseline_file):
    """`|| true` means a crash exits 0. Silence must not grade as green."""
    run_gate(tmp_path, baseline_file, {}, "", expected=1)


def test_no_summary_line_is_red(tmp_path, baseline_file):
    """A run that died before printing a summary proves nothing."""
    run_gate(
        tmp_path,
        baseline_file,
        {},
        "collected 900 items\n" + failed_line("tests/test_a.py::test_one"),
        expected=1,
    )


def test_interrupted_collection_is_red(tmp_path, baseline_file):
    run_gate(
        tmp_path,
        baseline_file,
        {},
        "!!!! Interrupted: 2 errors during collection !!!!" + summary(failed=0),
        expected=1,
    )


def test_internal_error_is_red(tmp_path, baseline_file):
    run_gate(
        tmp_path,
        baseline_file,
        {},
        "INTERNALERROR> Traceback (most recent call last):\n" + summary(failed=0),
        expected=1,
    )


def test_no_tests_ran_is_red(tmp_path, baseline_file):
    run_gate(
        tmp_path,
        baseline_file,
        {},
        "===== no tests ran in 0.4s =====\n",
        expected=1,
    )


# --- the log must agree with itself ---------------------------------------


def test_summary_reporting_more_failures_than_are_present_is_red(tmp_path, baseline_file):
    """The log lost its per-test FAILED lines but kept the epilogue.

    Every other trust check asks "does this look complete?". This one asks the
    question that actually matters. Grading a log like this as zero failures
    would be a GREEN GATE on a suite that broke.
    """
    run_gate(
        tmp_path,
        baseline_file,
        {},
        "collected 900 items\n===== 20 failed, 1345 passed, 45 skipped in 14m50s =====\n",
        expected=1,
    )


def test_summary_matching_the_observed_lines_is_green(tmp_path, baseline_file):
    """The companion case -- a self-consistent log must not trip the check."""
    run_gate(
        tmp_path,
        baseline_file,
        {"tests/test_a.py::test_one": 1, "tests/test_b.py::test_two": 1},
        failed_line("tests/test_a.py::test_one")
        + failed_line("tests/test_b.py::test_two")
        + summary(failed=2),
        expected=0,
    )


def test_reported_failure_count_reads_the_epilogue():
    assert gate.reported_failure_count(summary(failed=20, passed=1345)) == 20
    assert gate.reported_failure_count(summary(failed=0, passed=99)) == 0
    # No epilogue at all (pytest died before printing one).
    assert gate.reported_failure_count("collected 900 items\n") is None


def test_reported_failure_count_only_reads_the_tail():
    """A huge log's earlier text must not be mistaken for the epilogue."""
    log = "===== 999 failed, 0 passed in 1s =====\n" + ("noise line\n" * 20000) + summary(failed=2)
    assert gate.reported_failure_count(log) == 2


# --- false positives the gate must NOT raise ------------------------------


def test_word_interrupted_inside_a_test_name_is_not_fatal(tmp_path, baseline_file):
    """The fatal markers are anchored. A bare substring match on 'Interrupted'
    would fail any run containing test_interrupted_recovery."""
    run_gate(
        tmp_path,
        baseline_file,
        {"tests/test_a.py::test_interrupted_recovery": 1},
        failed_line("tests/test_a.py::test_interrupted_recovery") + summary(failed=1),
        expected=0,
    )


def test_duplicate_reporter_lines_do_not_inflate_the_count(tmp_path, baseline_file):
    """Enabling two pytest reporters prints each failure twice. Counting raw
    occurrences would report a 1->2 regression that does not exist."""
    line = failed_line("tests/test_a.py::test_one")
    run_gate(
        tmp_path,
        baseline_file,
        {"tests/test_a.py::test_one": 1},
        line + line + line + summary(failed=1),
        expected=0,
    )


def test_xfailed_marker_is_not_counted_as_a_failure():
    assert gate.parse_failures("XFAILED tests/test_a.py::test_one\n") == {}


# --- parsing --------------------------------------------------------------


def test_params_are_captured_then_canonicalised():
    counts = gate.parse_failures(
        failed_line("tests/test_a.py::test_one[x]")
        + failed_line("tests/test_a.py::test_one[y]")
        + failed_line("tests/test_b.py::test_two")
    )
    assert counts == {"tests/test_a.py::test_one": 2, "tests/test_b.py::test_two": 1}


def test_github_log_prefix_does_not_hide_failures():
    """Actions prefixes every line with `test\\tRun tests\\t<ts>\\t`, so `^FAILED`
    never matches. Anchoring on the token is what makes this work."""
    assert gate.parse_failures(failed_line("tests/test_a.py::test_one")) != {}


# --- baseline file validation --------------------------------------------


@pytest.mark.parametrize(
    "payload, reason",
    [
        ({"failures": ["tests/test_a.py::test_one"]}, "failures must be an object"),
        ({"failures": {"tests/test_a.py::test_one": 0}}, "zero is not an allowed count"),
        ({"failures": {"tests/test_a.py::test_one": True}}, "true is not a count"),
        ({"failures": {"tests/test_a.py::test_one": "1"}}, "a string is not a count"),
        ({"failures": {"no_node_id": 1}}, "keys must be pytest node ids"),
        ({"failures": {"tests/test_a.py::test_one[p]": 1}}, "keys must be canonical"),
        (
            {"expected_failure_count": 99, "failures": {"tests/test_a.py::test_one": 1}},
            "declared count must match the per-id counts",
        ),
    ],
)
def test_malformed_baseline_is_rejected(tmp_path, monkeypatch, payload, reason):
    """An unreadable or self-contradictory baseline must not silently green CI."""
    path = tmp_path / "baseline_failures.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(gate, "BASELINE_FILE", path)
    log = tmp_path / "pytest_output.txt"
    log.write_text(summary(failed=0), encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        gate.main(["check_test_baseline.py", str(log)])
    assert excinfo.value.code == 1, reason


def test_missing_baseline_file_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "BASELINE_FILE", tmp_path / "nope.json")
    log = tmp_path / "pytest_output.txt"
    log.write_text(summary(failed=0), encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        gate.main(["check_test_baseline.py", str(log)])
    assert excinfo.value.code == 1


@pytest.mark.parametrize("payload", [[], ["tests/test_a.py::test_one"], "failures", 7])
def test_baseline_that_is_not_a_json_object_is_rejected_cleanly(tmp_path, monkeypatch, payload):
    """A list or string at the top level must produce an explanation.

    Without the isinstance check, `"failures" not in data` is a legal
    membership test but `data["failures"]` then raises TypeError -- a raw
    traceback that tells the reader nothing about what to fix.
    """
    path = tmp_path / "baseline_failures.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(gate, "BASELINE_FILE", path)
    log = tmp_path / "pytest_output.txt"
    log.write_text(summary(failed=0), encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        gate.main(["check_test_baseline.py", str(log)])
    assert excinfo.value.code == 1


def test_real_baseline_file_is_self_consistent():
    """The committed baseline must satisfy its own validation rules.

    Cheap insurance that nobody hand-edits expected_failure_count without
    updating the per-id counts -- the gate would hard-fail every run with a
    confusing message instead of grading anything.
    """
    baseline, data = gate.load_baseline()
    assert baseline, "the committed baseline should not be empty"
    assert sum(baseline.values()) == data["expected_failure_count"]
    assert all(isinstance(v, int) and v >= 1 for v in baseline.values())