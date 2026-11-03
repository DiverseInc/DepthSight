# tests/test_pwa_oracle_honesty.py
"""
THE PWA MUST NOT FABRICATE AN ORACLE.
=====================================

WHY THIS FILE IS A SOURCE-LEVEL TEST, AND NOT A DOM TEST
---------------------------------------------------------
The customer-facing PWA has NO test runner. Measured in this checkout:

  * ``pwa/package.json`` declares no ``test`` script at all -- its scripts
    are ``dev``, ``build``, ``lint``, ``preview``. ``npm --prefix pwa test``
    exits non-zero with "Missing script: test".
  * ``pwa/package.json`` declares no vitest and no jest dependency, and no
    ``vitest.config.*`` / ``jest.config.*`` exists under ``pwa/``.
  * A search for ``pwa/**/*.{test,spec}.{ts,tsx,js}`` returns nothing.

Adding a test runner to the PWA would mean a new devDependency and a
``pwa/package-lock.json`` change -- a change to the packaging of the
customer-facing product, not to the honesty defect. It is out of scope for
this fix.

THE ALTERNATIVE USED, AND ITS LIMITS -- STATED PLAINLY
---------------------------------------------------------
The PWA's oracle DECISION is factored into a dependency-free module
(``pwa/lib/oracleCapability.ts``) and that module is exercised as real
BEHAVIOUR by the repository's only frontend runner, vitest, in
``frontend/src/__tests__/pwaOracleCapability.test.ts``. That test is the
guard on the decision.

What is left for THIS file is the WIRING: that the PWA's components do not
hard-code the fiction and do ask the server. That is asserted against the
source text, which is weaker than a rendered-DOM assertion and is labelled
as such here rather than dressed up as something it is not. It is not
vacuous: re-introducing `const canUseOracle = true`, or `useState(95)`, or
`|| 95`, makes this file fail.
"""

import re
from pathlib import Path

import pytest

PWA = Path(__file__).resolve().parents[1] / "pwa"

MODAL = PWA / "components" / "LaunchStrategyModal.tsx"
STRATEGIES_SCREEN = PWA / "screens" / "StrategiesScreen.tsx"
EDITOR_SCREEN = PWA / "screens" / "EditorScreen.tsx"
ORACLE_CAPABILITY = PWA / "lib" / "oracleCapability.ts"


def _source(path: Path) -> str:
    assert path.exists(), f"Expected file is missing: {path}"
    return path.read_text(encoding="utf-8")


def _code_only(source: str) -> str:
    """The source with its COMMENTS removed.

    These assertions are about CODE, not prose. The PWA's files explain the
    old fiction in comments -- on purpose, because that is where a reader
    learns what changed and why -- and a naive substring search would match
    the EXPLANATION of the defect instead of the defect. (This was not
    hypothetical: the first version of this file failed on its own comments.)

    Block comments (``/* ... */`` and the JSX ``{/* ... */}`` form) are
    dropped whole; a line whose first non-space characters are ``//`` is
    dropped whole. A TRAILING comment stays, so re-introducing the defect
    with a note next to it is still caught.
    """
    without_block_comments = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    code_lines = [
        line
        for line in without_block_comments.splitlines()
        if not line.lstrip().startswith("//")
    ]
    return "\n".join(code_lines)


@pytest.fixture(scope="module")
def modal_source() -> str:
    return _code_only(_source(MODAL))


@pytest.fixture(scope="module")
def strategies_source() -> str:
    return _code_only(_source(STRATEGIES_SCREEN))


@pytest.fixture(scope="module")
def editor_source() -> str:
    return _code_only(_source(EDITOR_SCREEN))


def test_the_pwa_never_hard_codes_oracle_availability(modal_source: str):
    """`canUseOracle` must come from the server, not from a constant.

    The constant it replaced was:
        const canUseOracle = true; // Oracle is available to everyone
    """
    assert "canUseOracle = true" not in modal_source, (
        "The PWA hard-codes the oracle as available. It must come from the "
        "server's /api/v1/capabilities, which reports whether a real model "
        "is actually loaded."
    )
    assert "useOracleAvailability" in modal_source, (
        "The launch modal no longer asks the server what it can do."
    )


def test_the_pwa_never_defaults_an_oracle_confidence(modal_source: str):
    """No `useState(95)` -- a confidence no model produced.

    95 was a CONSTANT. The api container logs, once per worker:
    "Oracle model not found at /app/data/oracle_model.joblib".
    """
    assert "useState(95)" not in modal_source, (
        "The PWA still starts the oracle confidence at 95. That number was "
        "never a model output."
    )
    assert not re.search(r"oracleConfidence[^\n]*\|\|\s*95", modal_source), (
        "The PWA still falls back to a confidence of 95 when there is none."
    )


def test_the_pwa_never_defaults_an_oracle_regime(modal_source: str):
    """No `useState("1")` -- a regime no model produced."""
    assert 'useState("1")' not in modal_source, (
        "The PWA still starts the oracle regime at 1, which claims a "
        "measurement that was never made."
    )
    assert not re.search(r"oracleRegime[^\n]*\|\|\s*\"1\"", modal_source), (
        "The PWA still falls back to regime 1 when there is none."
    )


def test_the_launch_payload_does_not_invent_oracle_numbers(
    strategies_source: str,
):
    """The numbers that reach the server must be the customer's own.

    The launch payload used to be built with:
        const regime = parseInt(details.oracleRegime || "1", 10);
        const confidence = details.oracleConfidence || 95;
    """
    assert not re.search(r"details\.oracleConfidence\s*\|\|\s*95", strategies_source), (
        "The launch payload still invents a 95% confidence."
    )
    assert not re.search(r"details\.oracleRegime\s*\|\|\s*\"1\"", strategies_source), (
        "The launch payload still invents oracle regime 1."
    )


def test_the_editor_does_not_preselect_an_oracle_regime(editor_source: str):
    """The strategy editor used to preselect regime 1 for a strategy that
    had never been given one."""
    assert not re.search(r"oracleRegime[^\n]*:\s*\"1\"", editor_source), (
        "The strategy editor still preselects oracle regime 1."
    )
    assert "useOracleAvailability" in editor_source, (
        "The strategy editor no longer asks the server whether the oracle "
        "exists."
    )


def test_the_oracle_decision_module_is_dependency_free():
    """The PWA's decision module must stay importable on its own.

    It is imported across the project boundary by the frontend's vitest
    suite (the PWA has no runner of its own). A runtime dependency here
    would break that test and hide the behaviour it guards.
    """
    source = _code_only(_source(ORACLE_CAPABILITY))
    imports = re.findall(r"^\s*import\s+.*$", source, flags=re.MULTILINE)
    assert imports == [], (
        f"pwa/lib/oracleCapability.ts must have no imports, found: {imports}"
    )
