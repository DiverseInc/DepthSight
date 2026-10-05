# tests/test_eval_state_key_ownership.py
"""
The evaluation-state panel must be readable by the strategy's OWNER.

`_publish_evaluation_state` wrote its Redis key under `self.user_id` -- the
user id of whichever controller happened to receive the candle event. The
read side scans `{PREFIX}:{current_user.id}:*`, i.e. the id resolved from the
authenticated user. Those two agree only as long as the evaluating controller
and the strategy's owner are always the same user, which is an assumption
about event routing rather than a fact carried with the data.

When they disagree, the write succeeds, nothing raises, and the strategy is
simply absent from its owner's panel forever -- exactly what was observed live
on strategy 6fcf5c10.

The START_STRATEGY payload carries the authoritative owner as `user_id`
(api/routes/strategies.py builds it from `current_user.id`). That is the
identity the key must be namespaced by.
"""

import ast
import inspect

from bot_module import controller as controller_module


def _eval_state_body() -> str:
    mod = ast.parse(inspect.getsource(controller_module))
    fn = next(
        n
        for n in ast.walk(mod)
        if isinstance(n, ast.AsyncFunctionDef)
        and n.name == "_publish_evaluation_state"
    )
    return ast.get_source_segment(inspect.getsource(controller_module), fn) or ""


def test_key_is_namespaced_by_the_strategy_owner_not_the_controller():
    body = _eval_state_body()
    assert "owner_user_id = config_dict.get(\"user_id\") or self.user_id" in body, (
        "the key must prefer the payload's user_id (the strategy's owner) and "
        "fall back to the controller's id"
    )


def test_key_does_not_use_the_bare_controller_user_id():
    """Guards the exact regression: a bare {self.user_id} in the key."""
    body = _eval_state_body()
    assert (
        'KEY_PREFIX}:{self.user_id}:{config_id}' not in body
    ), "the key is still namespaced by the controller's user id"


def test_a_missing_config_id_is_not_a_silent_return():
    """
    `if redis_client is None or not config_id: return` hid the case where a
    strategy could never appear in the panel. It now warns.
    """
    body = _eval_state_body()
    assert "cannot publish for a strategy with no config" in body, (
        "a missing config id must warn, not return silently -- the panel is "
        "the only place a user could ever learn their strategy is unpublishable"
    )


def test_publish_failures_are_not_swallowed_at_debug():
    """
    The function whose entire job is reporting problems logged its own
    failures at DEBUG, which production does not emit.
    """
    body = _eval_state_body()
    assert 'logger.debug(f"[EvalState] publish skipped' not in body, (
        "publish failures must not be logged at DEBUG"
    )
    assert "logger.warning" in body, "publish failures must be logged at WARNING"