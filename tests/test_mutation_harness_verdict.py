"""The mutation harness must decide "the mutant was caught" from the SUMMARY.

This is the regression pin for B4. The bug it pins: on the ``command`` path
the harness decided the verdict from the test command's EXIT CODE, so any
unrelated non-zero exit -- a collection error, an import failure, a
timeout, a bad command line -- was recorded as a false KILL. A false KILL
is the one thing this harness must never report, because it is what makes
a mutation-testing run look like it passed.

The harness is exercised END TO END here, against a COPY of itself in a
temporary directory, so the real working tree is never mutated:

  * a command that exits 0 but whose summary reports a failing test must
    be a KILL -- the summary is the authority, the exit code is not;
  * a command that exits non-zero but whose summary reports every test
    passing must NOT be a kill;
  * a command that produces no summary at all must be INCONCLUSIVE, never
    a kill, whatever it exits with.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "tools" / "mutation_harness.py"


def _load_harness_module():
    spec = importlib.util.spec_from_file_location("mutation_harness_under_test", HARNESS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_harness(
    tmp_path: Path,
    exit_code: int,
    stdout: str,
    clean_tree_must_pass: bool = False,
) -> str:
    """Run a COPY of the harness against a one-mutation spec.

    The copy lives in the temporary directory, so the harness's REPO -- which
    it derives from its own location -- is the temporary directory, and the
    real working tree is never touched.
    """
    (tmp_path / "tools").mkdir(parents=True, exist_ok=True)
    shutil.copy2(HARNESS, tmp_path / "tools" / "mutation_harness.py")

    target = tmp_path / "product_code.py"
    target.write_text("ORIGINAL = 1\n", encoding="utf-8")

    spec = {
        "command": [
            sys.executable,
            "-c",
            f"import sys; sys.stdout.write({stdout!r}); sys.exit({exit_code})",
        ],
        "timeout_s": 60,
        "mutations": [
            {
                "label": "probe: the mutant",
                "file": "product_code.py",
                "needle": "ORIGINAL = 1\n",
                "replace": "MUTATED = 1\n",
            }
        ],
    }
    if clean_tree_must_pass:
        spec["clean_tree_must_pass"] = True
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(tmp_path / "tools" / "mutation_harness.py"),
            str(spec_path),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    return proc.stdout + proc.stderr


def test_the_summary_helper_reads_the_summary_and_not_the_exit_code():
    module = _load_harness_module()

    # A failing test is a failure, whichever runner printed it.
    assert module.summary_reports_failure("Tests  1 failed | 26 passed (27)") is True
    assert module.summary_reports_failure("1 failed, 26 passed in 5.2s") is True
    # A file that could not even be loaded is reported by vitest on the
    # "Test Files" line, and by pytest as an error. Both are failures.
    assert module.summary_reports_failure("Test Files  1 failed (1) | Tests  no tests") is True
    # Everything passing is not a failure.
    assert module.summary_reports_failure("Tests  27 passed (27)") is False
    assert module.summary_reports_failure("Test Files  1 passed (1) | Tests  27 passed (27)") is False


def test_a_failing_summary_is_a_kill_even_when_the_command_exits_zero(tmp_path):
    """The summary is the authority. A zero exit code does not overrule it."""
    out = _run_harness(
        tmp_path,
        0,
        "Test Files  1 failed (1)\nTests  1 failed | 26 passed (27)\n",
    )

    assert "KILLED" in out, out
    assert "SURVIVED" not in out, out
    assert "INCONCLUSIVE" not in out, out


def test_a_non_zero_exit_is_not_a_kill_when_the_summary_says_everything_passed(tmp_path):
    """An unrelated non-zero exit must never be recorded as a KILL."""
    out = _run_harness(
        tmp_path,
        1,
        "Test Files  1 passed (1)\nTests  27 passed (27)\n",
    )

    assert "SURVIVED" in out, out
    assert "KILLED" not in out, out


def test_no_summary_is_inconclusive_and_never_a_kill(tmp_path):
    """No summary means no verdict. It is never a kill, whatever the exit code."""
    for exit_code in (0, 1):
        out = _run_harness(tmp_path, exit_code, "")

        assert "INCONCLUSIVE" in out, out
        assert "KILLED" not in out, out
        assert "SURVIVED" not in out, out


def test_a_command_that_fails_on_the_clean_tree_cannot_record_a_kill(tmp_path):
    """A kill has to be caused by the mutation.

    A command that reports a failure on the CLEAN tree cannot tell the
    mutant from the tree it started from, so every mutant it "catches" is a
    false kill -- and the run reports "RESULT: PASS -- every injected defect
    was caught by the tests" without a single test having run. A spec that
    asks for the clean-tree baseline must therefore be refused, not passed.
    """
    out = _run_harness(
        tmp_path,
        0,
        "Test Files  1 failed (1)\nTests  1 failed | 26 passed (27)\n",
        clean_tree_must_pass=True,
    )

    assert "SPEC ERROR" in out, out
    assert "KILLED" not in out, out
    assert "RESULT: PASS" not in out, out


def test_a_spec_without_the_baseline_says_that_it_trusts_its_command(tmp_path):
    """The trust is visible, not silent.

    A spec that does not ask for the clean-tree baseline still runs -- but
    the output says that the harness is trusting the command's summary
    without checking it, so nobody reads "RESULT: PASS" as more than it is.
    """
    out = _run_harness(tmp_path, 0, "Tests  27 passed (27)\n")

    assert "clean-tree baseline" in out, out
    assert "SURVIVED" in out, out
    assert "KILLED" not in out, out


def test_a_spec_that_injects_nothing_is_refused(tmp_path):
    """A spec with no mutations cannot be asked a question.

    A run of it reports "every injected defect was caught by the tests" about
    ZERO defects -- a green proof of nothing. That is the one way this harness
    can be made to say PASS without having been asked anything at all, so a
    spec that injects no defect is refused outright.
    """
    (tmp_path / "tools").mkdir(parents=True, exist_ok=True)
    shutil.copy2(HARNESS, tmp_path / "tools" / "mutation_harness.py")
    (tmp_path / "product_code.py").write_text("ORIGINAL = 1\n", encoding="utf-8")

    spec = {
        "command": [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('Tests  27 passed (27)\\n'); sys.exit(0)",
        ],
        "timeout_s": 60,
        "mutations": [],
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(tmp_path / "tools" / "mutation_harness.py"),
            str(spec_path),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    out = proc.stdout + proc.stderr

    assert "SPEC ERROR" in out, out
    assert "RESULT: PASS" not in out, out
    assert proc.returncode != 0, out
