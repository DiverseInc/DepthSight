from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


# THE MINIMUM ORACLE CONFIDENCE THE DYNAMIC_ORACLE FILTER APPLIES.
#
# 95 (percent) is NOT a new number and NOT a new policy. It is the
# threshold the clients used to send, before the honest-oracle change:
#
#     pwa/screens/StrategiesScreen.tsx   ->  details.oracleConfidence || 95
#     frontend/src/pages/Strategies.tsx  ->  formData.oracleConfidence || 95
#
# so the filter ran at `oracle_confidence >= 0.95`.
#
# The honest-oracle change replaced those sent values with 0 ("there is no
# oracle here"), and the filter read 0 as "no threshold at all":
#
#     min_confidence_raw = (self.symbol_selection_config.oracle_confidence or 0.0)
#     min_confidence = raw / 100 if raw > 1 else raw        # -> 0.0
#     ... if s.get("oracle_confidence", 0.0) >= min_confidence   # -> EVERY symbol
#
# That turned a ">= 0.95" filter into a NO-OP, in silence: the guard was
# weaker and nothing said so. A guard that silently disables a risk
# control is itself a critical bug (AGENTS.md, "Trading safety rules"), so
# "not chosen" must never mean "no threshold".
#
# It lives in this module -- the one that owns the oracle selection
# config -- so the filter and the capability report quote the SAME
# number and cannot drift apart.
DEFAULT_ORACLE_CONFIDENCE_PERCENT: float = 95.0


def effective_oracle_confidence_fraction(raw: Optional[float]) -> float:
    """The minimum oracle confidence (a FRACTION, 0-1) the filter applies.

    `raw` is the customer's setting in PERCENT (0-100), or None when the
    strategy has no confidence of its own.

    An ABSENT or ZERO setting is NOT "no threshold": it means "not
    chosen", and it falls back to the declared default (95% = 0.95) --
    the threshold the clients sent before the honest-oracle change.
    Reading it as 0.0 admits EVERY symbol and silently disables the
    filter, which is how the guard was weakened in the first place.

    A setting above 1 is a percentage (95 -> 0.95). A setting of 1 or
    below is already the fraction form the screener reports in
    `oracle_confidence`. That normalisation is unchanged: it is the rule
    the filter has always applied.
    """
    if raw is None or raw <= 0:
        return DEFAULT_ORACLE_CONFIDENCE_PERCENT / 100.0
    if raw > 1:
        return raw / 100.0
    return raw


def filter_symbols_by_oracle(
    symbols: List[Dict[str, Any]],
    required_regime: Optional[int],
    min_confidence: float,
) -> List[Dict[str, Any]]:
    """The DYNAMIC_ORACLE regime/confidence filter, as a pure function.

    `min_confidence` is a FRACTION (0-1), the same form the screener
    reports in `oracle_confidence`. `required_regime` is the regime the
    customer chose.

    Extracted out of `TradingController._dynamic_symbol_selection_loop`
    so the rule can be tested on its own: a copy of the rule in a test
    would only test the copy.
    """
    return [
        s
        for s in symbols
        if s.get("oracle_regime") == required_regime
        and s.get("oracle_confidence", 0.0) >= min_confidence
    ]


class SymbolSelectionConfig(BaseModel):
    mode: Literal["STATIC", "DYNAMIC_NATR", "DYNAMIC_ORACLE"] = "STATIC"
    min_natr: Optional[float] = Field(0.0, ge=0.0, le=10.0)
    oracle_regime: Optional[Literal[0, 1, 2]] = (
        None  # 0: Amnesia, 1: Paranoia, 2: Schizophrenia
    )
    oracle_confidence: Optional[float] = Field(0.0, ge=0.0, le=100.0)
    max_concurrent_symbols: int = Field(1, ge=1)

    @model_validator(mode="after")
    def validate_dynamic_modes(self):
        if self.mode == "DYNAMIC_NATR":
            if self.min_natr is None:
                raise ValueError("min_natr must be provided for DYNAMIC_NATR mode")
        elif self.mode == "DYNAMIC_ORACLE":
            if self.oracle_regime is None:
                raise ValueError(
                    "oracle_regime must be provided for DYNAMIC_ORACLE mode"
                )
            if self.oracle_confidence is None:
                raise ValueError(
                    "oracle_confidence must be provided for DYNAMIC_ORACLE mode"
                )
        return self
