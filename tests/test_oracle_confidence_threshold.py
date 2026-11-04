# tests/test_oracle_confidence_threshold.py
"""
THE ORACLE CONFIDENCE THRESHOLD MUST NOT VANISH.
=================================================

WHAT WAS WRONG
--------------
The honest-oracle change removed the hard-coded 95 from the clients --
correct -- but the clients then sent 0 instead, and the server read 0 as
"no threshold at all":

    # bot_module/controller.py, DYNAMIC_ORACLE branch
    min_confidence_raw = (self.symbol_selection_config.oracle_confidence or 0.0)
    min_confidence = raw / 100.0 if raw > 1 else raw      # -> 0.0
    ...
    if s.get("oracle_regime") == required_regime \\
       and s.get("oracle_confidence", 0.0) >= min_confidence

With ``min_confidence == 0.0`` the confidence test is true for EVERY
symbol in the regime. Before the change the clients sent 95, so the
filter ran at ``oracle_confidence >= 0.95``. The guard was silently
weakened, and nothing said so. AGENTS.md, "Trading safety rules": "A
guard that silently disables a risk control (blacklist, stop-loss,
emergency stop) is itself a critical bug -- fail closed and log loudly."

WHAT IS FIXED
-------------
* The server owns the threshold. ``DEFAULT_ORACLE_CONFIDENCE_PERCENT``
  (95) is the value the clients used to send -- NOT a new number and NOT
  a new policy -- and it lives in the module that owns the oracle
  selection config.
* An absent or zero confidence means "not chosen", and falls back to that
  declared default. It never means "no threshold".
* The rule is a pure function, so it can be tested on its own, and the
  controller calls THAT function rather than an inline copy.

THE TESTS BELOW PIN THE CUSTOMER-VISIBLE EFFECT
-----------------------------------------------
They do not assert that a constant changed. They assert that a strategy
with no confidence of its own still FILTERS -- that the symbols it
admits are the ones it admitted before this branch.
"""

import re
from pathlib import Path

from bot_module.symbol_selection import (
    DEFAULT_ORACLE_CONFIDENCE_PERCENT,
    SymbolSelectionConfig,
    effective_oracle_confidence_fraction,
    filter_symbols_by_oracle,
)

REPO = Path(__file__).resolve().parents[1]
CONTROLLER = REPO / "bot_module" / "controller.py"


def _screener_list() -> list[dict]:
    """A screener snapshot with two regimes and three confidences."""
    return [
        {"symbol": "AAA", "oracle_regime": 1, "oracle_confidence": 0.99},
        {"symbol": "BBB", "oracle_regime": 1, "oracle_confidence": 0.50},
        {"symbol": "CCC", "oracle_regime": 2, "oracle_confidence": 0.99},
    ]


def test_a_zero_confidence_does_not_admit_every_symbol():
    """THE GUARD. 0 must not mean "no threshold".

    Before the fix, a confidence of 0 became ``min_confidence = 0.0``
    and the filter admitted BBB (confidence 0.50) as well as AAA. It
    admits AAA only now.
    """
    min_confidence = effective_oracle_confidence_fraction(0)

    kept = filter_symbols_by_oracle(_screener_list(), 1, min_confidence)

    assert [s["symbol"] for s in kept] == ["AAA"], (
        "A confidence of 0 was read as 'no threshold' and the regime filter "
        "admitted every symbol in the regime. The guard was silently "
        "disabled. It must fall back to the declared threshold instead."
    )


def test_an_absent_confidence_does_not_admit_every_symbol():
    """The same for a strategy that has no confidence at all."""
    kept = filter_symbols_by_oracle(
        _screener_list(), 1, effective_oracle_confidence_fraction(None)
    )

    assert [s["symbol"] for s in kept] == ["AAA"], (
        "A strategy with no confidence was filtered by no threshold at all."
    )


def test_the_declared_default_is_the_threshold_the_clients_used_to_send():
    """95 percent = 0.95. The PRE-BRANCH effective threshold.

    The clients sent 95 before the honest-oracle change
    (``details.oracleConfidence || 95`` / ``formData.oracleConfidence ||
    95``), so the filter ran at 0.95. This constant restores exactly
    that, in ONE place, and it is NOT a new value.
    """
    assert DEFAULT_ORACLE_CONFIDENCE_PERCENT == 95.0
    assert effective_oracle_confidence_fraction(None) == 0.95
    assert effective_oracle_confidence_fraction(0) == 0.95


def test_a_stored_zero_confidence_is_the_declared_default():
    """What the DATABASE holds is 0.0 (the field's default).

    ``SymbolSelectionConfig.oracle_confidence`` defaults to 0.0, so a
    strategy that was never given a confidence stores 0.0. That must
    still be filtered at the declared threshold, not at nothing.
    """
    config = SymbolSelectionConfig(mode="DYNAMIC_ORACLE", oracle_regime=1)

    assert config.oracle_confidence == 0.0
    assert effective_oracle_confidence_fraction(config.oracle_confidence) == 0.95


def test_a_chosen_confidence_is_respected():
    """The customer's own choice is what is applied."""
    assert effective_oracle_confidence_fraction(60) == 0.60
    assert effective_oracle_confidence_fraction(95) == 0.95
    # A value of 1 or below is already the fraction form the screener
    # reports. That normalisation is unchanged.
    assert effective_oracle_confidence_fraction(0.5) == 0.5


def test_the_controller_applies_the_resolver_and_not_an_inline_copy():
    """The rule under test must be the rule that RUNS.

    A copy of the rule in a test only tests the copy, so the controller
    is checked to call the resolver and the filter -- and to no longer
    contain the ``or 0.0`` that turned "not chosen" into "no threshold".
    """
    source = CONTROLLER.read_text(encoding="utf-8")

    assert "effective_oracle_confidence_fraction(" in source, (
        "The controller no longer applies the oracle confidence threshold."
    )
    assert "filter_symbols_by_oracle(" in source, (
        "The controller no longer filters by the oracle regime."
    )
    assert not re.search(r"oracle_confidence\s*or\s*0\.0", source), (
        "The controller reads an absent confidence as 0.0 again -- 'no "
        "threshold' -- and the regime filter becomes a no-op."
    )
