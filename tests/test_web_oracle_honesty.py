# tests/test_web_oracle_honesty.py
"""
THE WEB CLIENT MUST NOT FABRICATE AN ORACLE.
=============================================

The honest-oracle change fixed the PWA and the HFT dashboard. It did
NOT fix the PRIMARY web client, which kept:

    frontend/src/components/strategies/LaunchStrategyModal.tsx
      const canUseOracle = isAdmin || isPro;   // Oracle is available to
                                                // admins and Pro users
      ...?.toString() || "1"                    // a regime
      (…oracle_settings?.confidence as number) || 95   // a confidence
      defaultValue={[field.value || 95]}       // ... shown to the customer

    frontend/src/pages/Strategies.tsx
      const regime = parseInt(formData.oracleRegime || "1", 10);
      const confidence = formData.oracleConfidence || 95;

on a deployment whose api container logs, ONCE PER WORKER:

    WARNING [api.simulation_router:122] - Oracle model not found at
    /app/data/oracle_model.joblib

There is no `train_oracle.py` in this repository, so nothing produces
that file: the model is not merely missing, it does not exist yet. The
95 the customer saw was a CONSTANT -- never a model output.

TWO AXES, NOT ONE
-----------------
The ENTITLEMENT says whether this CUSTOMER may use the oracle at all
(it is a paid feature). The AVAILABILITY says whether this DEPLOYMENT
has a model to filter with, and only the SERVER knows that. The old
code conflated them, so an entitled customer on a deployment with no
model was offered an oracle filter that could not exist.

WHY THIS FILE IS A SOURCE-LEVEL TEST
------------------------------------
The web client's components are React, and the repository's only
front-end runner is vitest, which lives in `frontend/`. The DECISION
logic is therefore factored into a dependency-free module
(`frontend/src/lib/oracleCapability.ts`) and exercised as real BEHAVIOUR
by `frontend/src/__tests__/webOracleCapability.test.ts`, which also
proves that the PWA and the web client agree.

What is left for THIS file is the WIRING: that the web components do
ask the server, do not hard-code the fiction, and do not substitute one
symbol-selection mode for another behind the customer's back. That is
asserted against the source text, which is weaker than a rendered-DOM
assertion and is labelled as such here rather than dressed up as
something it is not. It is not vacuous: re-introducing `|| 95`,
`|| "1"`, or `canUseOracle = isAdmin || isPro` makes this file fail.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

WEB_MODAL = REPO / "frontend/src/components/strategies/LaunchStrategyModal.tsx"
WEB_PAGE = REPO / "frontend/src/pages/Strategies.tsx"
WEB_EDITOR = REPO / "frontend/src/components/strategy-editor/ConfigAndLaunchPanel.tsx"
WEB_MODULE = REPO / "frontend/src/lib/oracleCapability.ts"


def _source(path: Path) -> str:
    assert path.exists(), f"Expected file is missing: {path}"
    return path.read_text(encoding="utf-8")


def _code_only(source: str) -> str:
    """The source with its COMMENTS removed.

    These assertions are about CODE, not prose. The web client's files
    explain the old fiction in comments -- on purpose, because that is
    where a reader learns what changed and why -- and a naive substring
    search would match the EXPLANATION of the defect instead of the
    defect. (This was not hypothetical: the first version of this file
    failed on its own comments.)

    Block comments (``/* ... */`` and the JSX ``{/* ... */}`` form) are
    dropped whole; a line whose first non-space characters are ``//`` is
    dropped whole. A TRAILING comment stays, so re-introducing the
    defect with a note next to it is still caught.
    """
    without_block_comments = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    code_lines = [
        line
        for line in without_block_comments.splitlines()
        if not line.lstrip().startswith("//")
    ]
    return "\n".join(code_lines)


def test_the_web_client_asks_the_server_about_the_oracle():
    """Every surface that touches the oracle asks the SERVER.

    The capability endpoint (`GET /api/v1/capabilities`) reports whether
    a real model is loaded. The client must read it, not assume it.
    """
    for path in (WEB_MODAL, WEB_PAGE, WEB_EDITOR):
        source = _code_only(_source(path))
        assert "useOracleAvailability()" in source, (
            f"{path.name} no longer asks the server what it can do. The "
            "oracle must never be hard-coded on the client."
        )


def test_the_web_client_never_invents_a_confidence():
    """No `|| 95` -- a confidence no model produced."""
    for path in (WEB_MODAL, WEB_PAGE, WEB_EDITOR):
        source = _code_only(_source(path))
        assert not re.search(r"oracleConfidence[^\n]*\|\|\s*95", source), (
            f"{path.name} still falls back to a confidence of 95 when there "
            "is none. That number was never a model output."
        )
        assert not re.search(r"defaultConfidence\s*=\s*\d", source), (
            f"{path.name} assigns a NUMBER to the oracle confidence "
            "threshold. The threshold must come from the server's "
            "declaration, not from a constant in the client."
        )
        assert "95" not in source or "oracleDefaultConfidence" in source, (
            f"{path.name} mentions a hard-coded 95. The threshold must come "
            "from the server's declaration."
        )


def test_the_web_client_never_invents_a_regime():
    """No `|| "1"` -- a regime no model produced."""
    for path in (WEB_MODAL, WEB_PAGE, WEB_EDITOR):
        source = _code_only(_source(path))
        assert not re.search(r"oracleRegime[^\n]*\|\|\s*\"1\"", source), (
            f"{path.name} still falls back to oracle regime 1 when there is "
            "none. The regime is the customer's to pick."
        )


def test_the_entitlement_and_the_availability_are_separate_axes():
    """`canUseOracle` may no longer BE the entitlement check.

    The old line -- `const canUseOracle = isAdmin || isPro;` -- was an
    entitlement check wearing the label of an availability check.
    """
    for path in (WEB_MODAL, WEB_EDITOR):
        source = _code_only(_source(path))
        assert not re.search(r"canUseOracle\s*=\s*isAdmin\s*\|\|", source), (
            f"{path.name} still decides the oracle's availability from the "
            "customer's plan. An entitled customer on a deployment with no "
            "model must not be offered the oracle."
        )
        assert "oracleEntitled" in source, (
            f"{path.name} does not keep the entitlement as its own axis."
        )
        assert "oracleAvailable" in source, (
            f"{path.name} does not read the server's answer."
        )
        assert re.search(r"canUseOracle\s*=\s*[^;]*oracleAvailable", source), (
            f"{path.name} decides `canUseOracle` without the server's "
            "answer. The availability and the entitlement are different "
            "axes and both must be in that decision."
        )


def test_the_launch_payload_is_built_from_the_servers_answer():
    """The numbers that reach the server come from the shared builder."""
    page = _code_only(_source(WEB_PAGE))
    assert "buildOracleLaunchSettings(" in page, (
        "The launch payload is not built from the server's answer, so it "
        "can carry a regime and a confidence nobody chose."
    )
    modal = _code_only(_source(WEB_MODAL))
    assert "buildOracleLaunchSettings" not in modal, (
        "The launch modal must not build the payload itself; the page owns "
        "it, so there is one answer."
    ) or "effectiveOracleConfidence" in modal


def test_a_saved_oracle_strategy_is_not_silently_relaunched_in_another_mode():
    """The launch must FAIL LOUDLY, not switch mode behind the customer.

    The PWA did exactly this:
        dynamicMode: canUseOracle ? dynamicMode : "DYNAMIC_NATR",
    so a strategy the customer saved as DYNAMIC_ORACLE was relaunched as
    a NATR strategy -- a different strategy, launched without a word.
    """
    for path in (WEB_MODAL,):
        source = _code_only(_source(path))
        assert "oracleLaunchRefusal" in source, (
            f"{path.name} does not refuse a saved oracle strategy when the "
            "oracle is gone; it substitutes another mode instead."
        )
        assert not re.search(r"dynamicMode:\s*canUseOracle", source), (
            f"{path.name} still substitutes the symbol-selection mode."
        )


def test_the_web_decision_module_is_dependency_free():
    """The web client's decision module must stay importable on its own.

    It is imported by the vitest suite that also proves the PWA and the
    web client agree. A runtime dependency here would break that test
    and hide the behaviour it guards.
    """
    source = _code_only(_source(WEB_MODULE))
    imports = re.findall(r"^\s*import\s+.*$", source, flags=re.MULTILINE)
    assert imports == [], (
        f"frontend/src/lib/oracleCapability.ts must have no imports, "
        f"found: {imports}"
    )
