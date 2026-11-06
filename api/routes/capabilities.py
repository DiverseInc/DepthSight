# api/routes/capabilities.py
#
# WHAT THIS DEPLOYMENT CAN ACTUALLY DO.
#
# A client must never have to guess whether a server-side capability
# exists. The oracle is the case that proves it:
#
#   * the PWA shipped `const canUseOracle = true; // Oracle is available to
#     everyone` and a 95% "Min Confidence" that no model ever produced;
#   * the api container logs, once per worker PID:
#       WARNING [api.simulation_router:122] - Oracle model not found at
#       /app/data/oracle_model.joblib
#   * there is no `train_oracle.py` in the repo, so nothing produces that
#     file -- the model is not merely missing, it does not exist yet.
#
# So the number the customer saw (95) was a CONSTANT, never a model output,
# and the screen told them a regime and a confidence that nothing produced.
#
# THIS ENDPOINT IS THE FIX ON THE SERVER SIDE: it reports the real state,
# and the clients read it instead of hard-coding it. The value is computed
# by `api.simulation_router.oracle_model_available()`, which is literally
# `get_oracle() is not None` -- the same call this API already makes to
# decide whether the oracle works. There is no second source of truth, so
# the flag cannot drift from reality, and it flips to True on its own the
# day a real model file is deployed.
#
# IT ALSO REPORTS THE ORACLE'S DECLARED CONFIDENCE THRESHOLD, because the
# clients must not invent one either. The threshold is a filter setting,
# not a model output: it is the minimum confidence a symbol must reach to
# be traded. The server owns it
# (`bot_module.symbol_selection.DEFAULT_ORACLE_CONFIDENCE_PERCENT`), the
# filter applies it, and the clients display and send back exactly what
# the server declares. 95 (percent) is the value the clients used to send
# before the honest-oracle change, so this is NOT a new number and NOT a
# new policy -- it is the previous effective behaviour, now in one place
# instead of hard-coded in three clients.

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from .. import models, schemas
from ..auth import get_current_user

logger = logging.getLogger(__name__)

capabilities_router = APIRouter(
    prefix="/api/v1",
    tags=["Capabilities"],
)


def _declared_default_confidence() -> float:
    """The oracle confidence threshold this deployment applies, in percent.

    Imported lazily for the same reason `oracle_model_available` is: the
    import must not make importing the API heavier than it was.
    """
    from bot_module.symbol_selection import DEFAULT_ORACLE_CONFIDENCE_PERCENT

    return DEFAULT_ORACLE_CONFIDENCE_PERCENT


class Capabilities(BaseModel):
    """What this deployment can actually do, right now.

    `oracle_available` is the honest answer to "is there an oracle model?".
    It is False on a deployment with no `data/oracle_model.joblib`, which
    is every deployment today: the model is not trained and not shipped.

    `oracle_default_confidence` is the minimum oracle confidence (in
    percent) the DYNAMIC_ORACLE filter applies when a strategy has not
    been given one of its own. It is the SAME number the filter uses
    (`bot_module.symbol_selection.DEFAULT_ORACLE_CONFIDENCE_PERCENT`), so
    a client that displays it and sends it back gets exactly the filter
    that is running.
    """

    oracle_available: bool = Field(
        default=False,
        alias="oracleAvailable",
        description=(
            "True only when a real oracle model is loaded and usable. "
            "False means no model exists, so no oracle regime and no oracle "
            "confidence may be shown to a customer."
        ),
    )
    oracle_default_confidence: float = Field(
        default_factory=_declared_default_confidence,
        alias="oracleDefaultConfidence",
        description=(
            "The minimum oracle confidence, in percent, that the "
            "DYNAMIC_ORACLE filter applies when a strategy has no "
            "confidence of its own. Zero means 'no threshold' nowhere: "
            "an absent or zero confidence falls back to this value, "
            "because a filter that accepts every symbol is not a filter."
        ),
    )

    model_config = ConfigDict(
        populate_by_name=True,
    )


@capabilities_router.get(
    "/capabilities",
    response_model=schemas.ApiResponseData[Capabilities],
    summary="Get what this deployment can actually do",
)
async def get_capabilities_endpoint(
    current_user: models.User = Depends(get_current_user),
):
    """Report the real capability state, so the client never has to guess.

    Authenticated like `/config`: the client that renders the capability is
    an authenticated user, and a failed probe must fail CLOSED (the client
    treats a missing flag as "the oracle is unavailable") rather than open.
    """
    # Imported lazily: `api.simulation_router` pulls in the backtester stack,
    # and this route must not make importing the API heavier than it was.
    from ..simulation_router import oracle_model_available

    try:
        oracle_available = oracle_model_available()
    except Exception as e:  # pragma: no cover - defensive
        logger.error(f"Capabilities: oracle availability probe failed: {e}")
        # Fail closed: an unknown state is NOT an available oracle.
        oracle_available = False

    return {
        "data": Capabilities(
            oracle_available=oracle_available,
        )
    }
