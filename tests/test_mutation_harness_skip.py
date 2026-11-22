# tests/test_mutation_harness_skip.py
"""
A SKIPPED TEST IS NOT A PASS.
=============================

`tools/mutation_harness.py` exists to tell the difference between "the
mutant was caught" and "the mutant was not caught". On the front-end
(`command`) path it decides that from the test command's EXIT CODE. A
vitest run in which every test is SKIPPED still exits 0:

    Tests  139 skipped (139)

so the mutant would be recorded as SURVIVED -- a false green. The
harness now reports such a run as BROKEN and aborts, which is what the
spec's comment claims.

This test pins the rule itself. It does not run the harness: a unit test
of the decision is deterministic and takes milliseconds, where an
end-to-end run would need a whole vitest invocation.

The spec comment that made this claim is checked here too, because a
comment that overclaims is how this repo was bitten twice already
(PR #56 round 1). The comment must state the rule that is actually
implemented.
"""

import json
import re
from pathlib import Path

from tools.mutation_harness import summary_ran_no_test

REPO = Path(__file__).resolve().parents[1]
FRONTEND_SPEC = REPO / "tools/mutations/oracle_availability_frontend.json"


def test_a_run_in_which_every_test_was_skipped_ran_no_test():
    assert summary_ran_no_test("Tests  139 skipped (139)") is True


def test_a_run_which_executed_some_tests_is_not_a_skip():
    assert summary_ran_no_test("Tests  2 failed | 137 passed | 1 skipped (140)") is False
    assert summary_ran_no_test("Tests  139 passed (139)") is False
    assert summary_ran_no_test("Test Files  22 passed (22)") is False


def test_a_pytest_summary_is_never_mistaken_for_a_skip():
    assert summary_ran_no_test("33 passed, 4 warnings in 25.94s") is False
    assert summary_ran_no_test("1 failed, 32 passed in 25.94s") is False


def test_the_front_end_spec_states_the_rule_it_relies_on():
    """The comment must not overclaim.

    The old comment said the command path keeps "a SKIP counts as a
    failure". That is only true because the harness now treats an
    all-skipped run as BROKEN. The comment must say so, and must not
    claim anything the harness does not do.
    """
    spec = json.loads(FRONTEND_SPEC.read_text(encoding="utf-8"))
    comment = "\n".join(spec["_comment"])

    assert "skipped" in comment.lower(), (
        "The front-end spec's comment no longer mentions the skip rule at "
        "all, so a reader cannot tell what the harness guarantees."
    )
    assert re.search(r"all[- ]skipped", comment, flags=re.IGNORECASE), (
        "The front-end spec's comment must state the limit honestly: an "
        "all-skipped run is a BROKEN run, not a pass."
    )
    assert "SURVIVED" in comment, (
        "The front-end spec's comment must say what would otherwise "
        "happen to an all-skipped run."
    )
