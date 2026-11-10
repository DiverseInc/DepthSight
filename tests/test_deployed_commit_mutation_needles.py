# tests/test_deployed_commit_mutation_needles.py
"""Every needle in the deployed-commit mutation specs must match ONE site.

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

WHY THE PARAMETRISATION IS DYNAMIC
----------------------------------
The first version of this file hard-coded `@pytest.mark.parametrize("index",
range(5))` over ONE spec. Two things then escaped it:

  * a SIXTH mutation added to that spec was never checked -- the range said
    five, so the sixth needle was never measured, and a drifted needle would
    have made the harness SKIP that mutant (a skip is not a pass);
  * the whole of `deployed_commit_script.json` was not covered at all, so
    none of its needles were ever measured.

So the (spec, index) pairs are now COMPUTED from the spec files at collection
time: a spec that grows is covered the moment it grows, and a spec that is
added is covered without editing this file.

THE LIMIT IS DELIBERATE
------------------------
This file covers the DEPLOYED-COMMIT specs only. It is NOT extended to the
other eighteen specs in `tools/mutations`: three of those are known-drifted on
`main` and are being repaired on a separate branch, so pulling them in here
would make this file RED for reasons that have nothing to do with the
deployed-commit work, and would hide a real regression behind known drift.

This file is deliberately NOT in any spec's `pytest_target`: it reads the
mutated file, so running it DURING a mutation would make the uniqueness
assertion fail for the mutant rather than for the code. There is a test below
that enforces that.
"""

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MUTATIONS_DIR = REPO_ROOT / "tools" / "mutations"

THIS_FILE = "tests/test_deployed_commit_mutation_needles.py"

# The specs whose needles are checked here. Both deployed-commit specs are
# covered, every mutation in each, and so is the spec that defends the image
# and the certificate checks.
#
# The other 18 specs are NOT covered, and that limit is deliberate: three of
# them are known-drifted on `main` and are being repaired on a separate
# branch. Covering them before that repair lands would make this test file
# red for a reason that has nothing to do with the code it is judging.
SPEC_NAMES = (
    "source_offer_deployed_commit.json",
    "deployed_commit_script.json",
    "source_offer_image_and_certificate.json",
    "dockerignore_tracked_files.json",
)


def _variants(needle: str) -> list:
    """The needle in both line-ending forms, CRLF first -- as the harness does."""
    lf = needle.replace("\r\n", "\n").encode("utf-8")
    return [lf.replace(b"\n", b"\r\n"), lf]


def _spec_path(spec_name: str) -> Path:
    return MUTATIONS_DIR / spec_name


def _load(spec_name: str) -> dict:
    """The parsed spec, read fresh from disk."""
    path = _spec_path(spec_name)
    assert path.is_file(), f"the mutation spec must exist: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _mutation_pairs() -> list:
    """Every (spec_name, index) pair, computed from the spec files themselves.

    No count is hard-coded anywhere: a spec that grows is covered the moment
    it grows.
    """
    pairs = []
    for spec_name in SPEC_NAMES:
        path = _spec_path(spec_name)
        if not path.is_file():
            continue
        mutations = json.loads(path.read_text(encoding="utf-8")).get("mutations", [])
        for index in range(len(mutations)):
            pairs.append((spec_name, index))
    return pairs


def pytest_generate_tests(metafunc):
    """Parametrise the per-mutation test over every mutation of every spec.

    Only the test that asks for `index` is parametrised here: the other tests
    in this file are parametrised over the specs by their own mark, and
    parametrising `spec_name` twice would be a collection error.
    """
    if "index" not in metafunc.fixturenames:
        return
    pairs = _mutation_pairs()
    metafunc.parametrize(
        "spec_name,index",
        pairs,
        ids=[f"{name}[{index}]" for name, index in pairs],
    )


@pytest.mark.parametrize("spec_name", SPEC_NAMES)
def test_the_spec_names_at_least_one_mutation(spec_name: str):
    path = _spec_path(spec_name)
    assert path.is_file(), f"the mutation spec must exist: {path}"
    assert _load(spec_name).get("mutations"), "the spec carries no mutations"


@pytest.mark.parametrize("spec_name", SPEC_NAMES)
def test_the_spec_targets_test_files_that_exist(spec_name: str):
    for target in _load(spec_name)["pytest_target"].split():
        assert (REPO_ROOT / target).is_file(), (
            f"the spec's pytest_target names a file that does not exist: {target}"
        )


@pytest.mark.parametrize("spec_name", SPEC_NAMES)
def test_this_file_is_not_in_the_specs_pytest_target(spec_name: str):
    """This file reads the mutated file, so it must not run DURING a mutation.

    If it were in the target, the uniqueness assertion would fail for the
    MUTANT rather than for the code, and the harness would report a killed
    mutant for the wrong reason.
    """
    targets = _load(spec_name)["pytest_target"].split()
    assert THIS_FILE not in targets, (
        f"{THIS_FILE} reads the mutated file, so it must not be in the "
        f"pytest_target of {spec_name}: {targets}"
    )


def test_every_needle_matches_exactly_one_site(spec_name: str, index: int):
    mutations = _load(spec_name)["mutations"]
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
        f"mutation {index} of {spec_name} ({mutation['label']}): its needle "
        f"does not occur in {mutation['file']} -- the spec has drifted and the "
        "mutation would be SKIPPED, which is not a pass"
    )

    assert data.count(chosen) == 1, (
        f"mutation {index} of {spec_name} ({mutation['label']}): its needle "
        f"occurs {data.count(chosen)} times in {mutation['file']}. A needle "
        "that matches more than one site makes the mutation meaningless, "
        "because the harness replaces only the first one."
    )


@pytest.mark.parametrize("spec_name", SPEC_NAMES)
def test_every_mutation_replaces_with_something_different(spec_name: str):
    for mutation in _load(spec_name)["mutations"]:
        assert mutation["needle"] != mutation["replace"], (
            f"mutation ({mutation['label']}) replaces a string with itself"
        )


@pytest.mark.parametrize("spec_name", SPEC_NAMES)
def test_every_mutation_names_a_file_that_exists(spec_name: str):
    for mutation in _load(spec_name)["mutations"]:
        assert (REPO_ROOT / mutation["file"]).is_file(), (
            f"mutation ({mutation['label']}) names a file that does not "
            f"exist: {mutation['file']}"
        )
