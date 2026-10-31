"""The registry must hold only the strategies this server can ACTUALLY TRADE.

D6 -- the strategy library was decorative in a way nobody could see.

``CompassStrategy`` lives in its own module (``bot_module/compass_strategy``)
and trades on a TRAINED model. The model file is not in the repository --
``data/`` is gitignored, no script in the repo produces it, and it is
bind-mounted onto the server from the host
(``docker-compose.yml``: ``./data:/app/data``, on the ``api`` and ``bot``
services alike, so the API and the bot always see the same file).

Where the file is absent, ``CompassStrategy.__init__`` logs an error and
leaves ``self.compass_model`` as None, and ``check_signal`` then returns no
signal FOREVER::

    # bot_module/compass_strategy.py
    if not self.compass_model:
        return None, 0.0, None

So the strategy STARTS everywhere and TRADES only where its model is
installed. Before this change the registry verified only that a candidate
could be CONSTRUCTED, so a strategy that starts and then does nothing was
offered to clients as though it were a working strategy -- the exact thing
the registry exists to prevent ("a client who picked it is left looking at a
product that silently does nothing", ``bot_module/strategy.py``).

The rule now is: **a strategy enters the registry only if it can actually
trade on this machine.** The check is a runtime check, so it is true in both
directions -- on a server that HAS the model Compass is offered and trades;
on a server that does not, it is not offered at all, and the reason names the
file that is missing.

These tests are deliberately ENVIRONMENT-INDEPENDENT: they assert the
INVARIANT, never the literal presence of a name, because a test that depends
on a deployment artifact is a test that lies on the machine it does not run
on. (The registry is built at import time, so a model installed after start-up
needs a restart -- that is stated in the module docstring below.)
"""

import subprocess
import sys
from pathlib import Path

import pytest

from bot_module import config as bot_config
from bot_module.strategy import STRATEGIES, _verify_registry_candidate
from bot_module.strategy_registry import CLIENT_SELECTABLE_STRATEGIES


def _compass_without_models(tmp_path: Path, monkeypatch) -> None:
    """Point Compass at model files that are definitely not there."""
    monkeypatch.setattr(
        bot_config, "COMPASS_MODEL_PATH", tmp_path / "no-compass.json", raising=False
    )
    monkeypatch.setattr(
        bot_config, "ORACLE_MODEL_PATH", tmp_path / "no-oracle.joblib", raising=False
    )


# --- the rule itself --------------------------------------------------------


def test_a_strategy_without_its_model_is_refused_with_the_reason(
    tmp_path, monkeypatch
):
    """RED for the fix: the refusal must happen, and it must NAME the file.

    A refusal with no reason is a silent no-op, and a silent no-op is how the
    registry went on offering a strategy that could not trade.
    """
    from bot_module.compass_strategy import CompassStrategy

    _compass_without_models(tmp_path, monkeypatch)

    reason = _verify_registry_candidate(CompassStrategy)
    assert reason is not None, (
        "a strategy with no model must not pass the verification -- it starts "
        "cleanly and then never signals"
    )
    assert "compass_model" in reason, reason
    assert str(tmp_path / "no-compass.json") in reason, reason


def test_a_strategy_with_its_model_is_accepted(tmp_path, monkeypatch):
    """GREEN for the fix: with its model present, the strategy is operable."""
    from bot_module.compass_strategy import CompassStrategy

    _compass_without_models(tmp_path, monkeypatch)

    def fake_load(self):
        self.compass_model = object()
        self.oracle_model = object()
        self.feature_names = ["pressure_buy"]

    monkeypatch.setattr(CompassStrategy, "_load_models", fake_load)

    assert _verify_registry_candidate(CompassStrategy) is None


def test_a_strategy_that_needs_nothing_extra_is_operational():
    """The default must be operational, or every strategy would be refused."""
    from bot_module.strategy import VolumeBreakoutStrategy

    instance = VolumeBreakoutStrategy(params=None, contract_id=None)
    assert instance.operational_problem() is None


# --- the invariant the client depends on -------------------------------------


def test_every_strategy_the_client_may_pick_can_actually_trade():
    """A name in the client's menu is a name the server can run.

    This is the invariant that replaces "CompassStrategy is in the list". It
    holds whatever this machine has, and it is the one the client needs.
    """
    offenders = {}
    for name in CLIENT_SELECTABLE_STRATEGIES:
        reason = _verify_registry_candidate(STRATEGIES[name])
        if reason is not None:
            offenders[name] = reason
    assert not offenders, (
        "these strategies are offered to clients but cannot actually trade: %s"
        % offenders
    )


def test_the_client_list_is_exactly_the_runtime_registry():
    """No strategy the runtime can run may be refused, and vice versa."""
    assert set(CLIENT_SELECTABLE_STRATEGIES) == set(STRATEGIES.keys())


def test_compass_is_offered_exactly_when_it_can_trade():
    """The registry and the operator must not disagree about Compass.

    Whether this server has the trained model is a property of the DEPLOYMENT,
    not of the code, so the test asks the question both ways instead of
    asserting one answer.
    """
    from bot_module.compass_strategy import CompassStrategy

    reason = _verify_registry_candidate(CompassStrategy)
    offered = "CompassStrategy" in CLIENT_SELECTABLE_STRATEGIES

    if reason is None:
        assert offered, "Compass can trade here, so it must be offered"
    else:
        assert not offered, (
            "Compass cannot trade here (%s), so it must not be offered" % reason
        )


def test_a_fresh_interpreter_agrees_with_itself():
    """The API process and the bot process must reach the SAME conclusion.

    Measured in a FRESH interpreter, because the defect was an import-order
    defect: the registry is built at import time, and the API process never
    imports bot_runner.
    """
    script = (
        "import bot_module.strategy_registry as sr;"
        "from bot_module.strategy import _verify_registry_candidate;"
        "print('SETS_MATCH', set(sr.CLIENT_SELECTABLE_STRATEGIES) == set(sr.STRATEGIES.keys()));"
        "bad = [(n, _verify_registry_candidate(sr.STRATEGIES[n])) for n in sr.CLIENT_SELECTABLE_STRATEGIES "
        "if _verify_registry_candidate(sr.STRATEGIES[n]) is not None];"
        "print('OFFERED_BUT_DEAD', bad);"
        "print('COMPASS_OFFERED', 'CompassStrategy' in sr.CLIENT_SELECTABLE_STRATEGIES)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
    assert "SETS_MATCH True" in result.stdout, result.stdout
    assert "OFFERED_BUT_DEAD []" in result.stdout, result.stdout


@pytest.mark.parametrize(
    "name",
    [
        "VolumeBreakout",
        "FakeBreakout",
        "DensityBounce",
        "ConsolidationImpulse",
        "AggTradeReversal",
        "FirstPullbacksInTrend",
        "ReverseVolumeBreakout",
        "ReverseFakeBreakout",
        "VisualBuilderStrategy",
        "GeneticStrategy",
    ],
)
def test_the_strategies_the_product_sells_are_operational(name):
    """The ten strategies that do not need a trained artifact must all pass.

    This is the part of the library the $499 service is actually selling. If
    one of these ever stops being operable, the client must not be offered it.
    """
    assert name in STRATEGIES, name
    assert _verify_registry_candidate(STRATEGIES[name]) is None, name
