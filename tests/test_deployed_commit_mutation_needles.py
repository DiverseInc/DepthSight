# tests/test_deployed_commit_mutation_needles.py
"""Every needle in the deployed-commit mutation spec must match ONE site.

THE DEFECT THIS PINS
--------------------
A mutation needle that matches two sites is a mutation that proves nothing.
`tools/mutation_harness.py` replaces the FIRST occurrence it finds and then
runs the tests: if the needle occurs twice in the file, the second occurrence
-- the one the tests actually pin -- is still the original code, and the
mutant can survive for reasons that have nothing to do with the defect.

The harness does not check this itself, so it is checked here, against the
same rule the harness applies: take the CRLF variant of the needle first, then
the LF one, use the first variant that is found, and require that it occurs
EXACTLY ONCE in the file it is aimed at.

This file is deliberately NOT in the spec's `pytest_target`: it reads the
mutated file, so running it DURING a mutation would make the uniqueness
assertion fail for the mutant rather than for the code.
"""

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC = REPO_ROOT / "tools" / "mutations" / "source_offer_deployed_commit.json"


def _variants(needle: str) -> list:
    """The needle in both line-ending forms, CRLF first -- as the harness does."""
    lf = needle.replace("\r\n", "\n").encode("utf-8")
    return [lf.replace(b"\n", b"\r\n"), lf]


@pytest.fixture(scope="module")
def spec() -> dict:
    assert SPEC.is_file(), f"the mutation spec must exist: {SPEC}"
    return json.loads(SPEC.read_text(encoding="utf-8"))


def test_the_spec_names_at_least_one_mutation(spec: dict):
    assert spec.get("mutations"), "the spec carries no mutations"


def test_the_spec_targets_test_files_that_exist(spec: dict):
    for target in spec["pytest_target"].split():
        assert (REPO_ROOT / target).is_file(), (
            f"the spec's pytest_target names a file that does not exist: {target}"
        )


@pytest.mark.parametrize("index", range(5))
def test_every_needle_matches_exactly_one_site(spec: dict, index: int):
    mutations = spec["mutations"]
    assert index < len(mutations), f"the spec has no mutation {index}"

    mutation = mutations[index]
    target = REPO_ROOT / mutation["file"]
    assert target.is_file(), f"the mutation names a file that does not exist: {target}"

    data = target.read_bytes()

    chosen = None
    for variant in _variants(mutation["needle"]):
        if variant in data:
            chosen = variant
            break

    assert chosen is not None, (
        f"mutation {index} ({mutation['label']}): its needle does not occur in "
        f"{mutation['file']} -- the spec has drifted and the mutation would "
        "be SKIPPED, which is not a pass"
    )

    assert data.count(chosen) == 1, (
        f"mutation {index} ({mutation['label']}): its needle occurs "
        f"{data.count(chosen)} times in {mutation['file']}. A needle that "
        "matches more than one site makes the mutation meaningless, because "
        "the harness replaces only the first one."
    )


def test_every_mutation_replaces_with_something_different(spec: dict):
    for mutation in spec["mutations"]:
        assert mutation["needle"] != mutation["replace"], (
            f"mutation ({mutation['label']}) replaces a string with itself"
        )
