# tests/test_oracle_availability.py
"""
THE ORACLE MUST BE HONEST WITH THE CLIENT.
==========================================

WHAT WAS WRONG
--------------
The clients answered "is the oracle available?" themselves, with a constant.
The PWA shipped::

    const canUseOracle = true; // Oracle is available to everyone
    const [oracleRegime, setOracleRegime] = useState("1");
    const [oracleConfidence, setOracleConfidence] = useState(95);

and then handed those numbers to the server at launch::

    const regime = parseInt(details.oracleRegime || "1", 10);
    const confidence = details.oracleConfidence || 95;

On a deployment whose api container logs, ONCE PER WORKER::

    WARNING [api.simulation_router:122] - Oracle model not found at
    /app/data/oracle_model.joblib

There is no `train_oracle.py` in this repository, so nothing produces that
file: the model is not merely missing, it does not exist yet. The 95 the
customer saw was a CONSTANT -- never a model output -- and the screen told
them a regime and a confidence that nothing had produced.

WHAT IS FIXED
-------------
The SERVER now reports the real state on ``GET /api/v1/capabilities``:

    {"data": {"oracleAvailable": false}}

and the value is computed by ``api.simulation_router.oracle_model_available()``,
which is literally ``get_oracle() is not None`` -- the same call this API
already makes to decide whether the oracle works at all. There is no second
source of truth to drift from, and it flips to True on its own the day a
real ``data/oracle_model.joblib`` is deployed.

THESE TESTS PIN BOTH DIRECTIONS
--------------------------------
Not just "false when there is no model" (which a hard-coded ``False`` would
also pass): the flag must ALSO become True the moment a real, working model
file exists. That is what makes the endpoint an honest capability report
rather than another constant.
"""

from pathlib import Path

import joblib
import numpy as np
import pytest
from fastapi.testclient import TestClient
from sklearn.mixture import GaussianMixture

from api import models, simulation_router
from api.auth import get_current_user
from api.depthsight_api import app


@pytest.fixture
def client():
    """An authenticated client. Auth is bypassed, not the capability."""
    user = models.User(
        id="test-user-id",
        username="tester",
        plan="pro",
        hashed_password="hash",
        email="test@example.com",
        level=1,
        xp=0,
    )
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def real_oracle_model(tmp_path: Path) -> Path:
    """A REAL, working oracle model file.

    This is the thing production does not have. It is a genuine Gaussian
    Mixture dumped with joblib -- the same shape `bot_module.oracle.Oracle`
    loads -- so the "available" case exercises the real load path rather
    than a mocked one.
    """
    model_path = tmp_path / "oracle_model.joblib"
    features = np.random.rand(100, 3)
    model = GaussianMixture(n_components=3, random_state=42)
    model.fit(features)
    joblib.dump(model, model_path)
    return model_path


@pytest.fixture
def oracle_points_at(monkeypatch):
    """Point the API's oracle at a chosen model path, from scratch.

    `get_oracle()` caches its instance in a module global, so the singleton
    is reset as well as the path -- otherwise the second test in this file
    would be answered by the first test's model.
    """

    def _point_at(path: Path):
        monkeypatch.setattr(simulation_router, "ORACLE_MODEL_PATH", path)
        monkeypatch.setattr(simulation_router, "_oracle_instance", None)

    return _point_at


def test_capabilities_report_the_oracle_unavailable_when_no_model_exists(
    client, oracle_points_at, tmp_path
):
    """The production state: no model file -> the client is told NO."""
    oracle_points_at(tmp_path / "oracle_model.joblib")  # never created

    response = client.get("/api/v1/capabilities")

    assert response.status_code == 200, response.text
    body = response.json()
    assert "data" in body, body
    assert body["data"]["oracleAvailable"] is False, (
        "The server claimed an oracle exists while the model file does not. "
        "The client would then show a regime and a confidence that no model "
        "produced."
    )


def test_capabilities_report_the_oracle_available_when_a_real_model_exists(
    client, oracle_points_at, real_oracle_model
):
    """The future state: a real model file -> the client is told YES.

    A hard-coded False would pass the previous test and fail this one.
    """
    oracle_points_at(real_oracle_model)

    response = client.get("/api/v1/capabilities")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data"]["oracleAvailable"] is True, (
        "A real, working oracle model was deployed and the server still said "
        "it was unavailable. The oracle UI would stay switched off forever."
    )


def test_the_capability_follows_the_real_file_not_a_constant(
    client, oracle_points_at, tmp_path, real_oracle_model
):
    """The flag tracks the file: absent -> False, present -> True.

    This is the anti-drift test. It fails if the endpoint ever starts
    answering from a constant, a cached value, or anything other than the
    model file that is actually on disk.
    """
    missing = tmp_path / "not_deployed_yet.joblib"
    oracle_points_at(missing)
    assert client.get("/api/v1/capabilities").json()["data"]["oracleAvailable"] is False

    oracle_points_at(real_oracle_model)
    assert client.get("/api/v1/capabilities").json()["data"]["oracleAvailable"] is True


def test_oracle_model_available_agrees_with_get_oracle(monkeypatch, tmp_path):
    """The helper IS the runtime state, not a re-implementation of it.

    `oracle_model_available()` must be exactly `get_oracle() is not None`, so
    the endpoint can never answer something different from what the API
    would actually do with the oracle.
    """
    monkeypatch.setattr(
        simulation_router, "ORACLE_MODEL_PATH", tmp_path / "absent.joblib"
    )
    monkeypatch.setattr(simulation_router, "_oracle_instance", None)

    assert simulation_router.oracle_model_available() is False

    sentinel = object()
    monkeypatch.setattr(simulation_router, "_oracle_instance", sentinel)
    assert simulation_router.oracle_model_available() is True


def test_the_capability_fails_closed_when_the_probe_errors(client, monkeypatch):
    """An unknown state is NOT an available oracle.

    If the probe raises, the answer must be "no". The opposite would turn a
    transient failure into a customer-facing fiction.
    """

    def _boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(simulation_router, "oracle_model_available", _boom)

    response = client.get("/api/v1/capabilities")

    assert response.status_code == 200, response.text
    assert response.json()["data"]["oracleAvailable"] is False


def test_the_flag_stops_claiming_a_model_whose_file_is_gone(
    client, oracle_points_at, real_oracle_model
):
    """A model that has been DELETED must not be reported as available.

    The model file can be removed while the worker runs -- a deploy that
    swaps it, an operator that cleans it up. The flag used to keep
    reporting "available" because the loaded instance stayed cached in
    the worker, so the server could claim a model that was gone.

    The docstring of `oracle_model_available()` used to promise that
    "get_oracle() re-checks the model path on every call". It did not,
    in that direction. Now it does, in both.
    """
    oracle_points_at(real_oracle_model)
    assert (
        client.get("/api/v1/capabilities").json()["data"]["oracleAvailable"]
        is True
    ), "A real model was deployed and the server did not report it."

    real_oracle_model.unlink()

    assert (
        client.get("/api/v1/capabilities").json()["data"]["oracleAvailable"]
        is False
    ), (
        "The server still reports an available oracle after the model file "
        "was deleted. The flag can report a model that is gone."
    )


def test_the_capability_reports_the_declared_confidence_threshold(
    client, oracle_points_at, tmp_path
):
    """The client is told the threshold this deployment applies.

    The threshold is a filter setting, not a model output, and the
    server owns it. The clients display it and send it back, so the
    number on screen is the number the filter uses.
    """
    oracle_points_at(tmp_path / "absent.joblib")

    body = client.get("/api/v1/capabilities").json()["data"]

    assert body["oracleDefaultConfidence"] == 95.0, (
        "The server does not report its declared oracle confidence "
        "threshold, so the client has to invent one."
    )


def test_the_reported_threshold_is_the_threshold_the_filter_applies(
    client, oracle_points_at, tmp_path
):
    """The capability report and the filter quote the SAME number."""
    from bot_module.symbol_selection import (
        DEFAULT_ORACLE_CONFIDENCE_PERCENT,
        effective_oracle_confidence_fraction,
    )

    oracle_points_at(tmp_path / "absent.joblib")
    reported = client.get("/api/v1/capabilities").json()["data"][
        "oracleDefaultConfidence"
    ]

    assert reported == DEFAULT_ORACLE_CONFIDENCE_PERCENT
    assert effective_oracle_confidence_fraction(None) * 100 == reported


def test_the_hft_config_default_follows_the_servers_own_answer(
    monkeypatch, tmp_path, real_oracle_model
):
    """`HftConfig.use_oracle` used to be the constant True.

    The client already derives its own availability from the server's
    capability report, so a server-side default that says True while
    the server says the oracle does not exist says the opposite of the
    server's own answer.
    """
    from api import hft_router

    monkeypatch.setattr(
        simulation_router, "ORACLE_MODEL_PATH", tmp_path / "absent.joblib"
    )
    monkeypatch.setattr(simulation_router, "_oracle_instance", None)

    assert hft_router.HftConfig().use_oracle is False, (
        "With no oracle model on this deployment the server's own default "
        "still says the oracle filter is on."
    )

    monkeypatch.setattr(simulation_router, "ORACLE_MODEL_PATH", real_oracle_model)
    monkeypatch.setattr(simulation_router, "_oracle_instance", None)

    assert hft_router.HftConfig().use_oracle is True, (
        "A real oracle model is loaded and the server's own default still "
        "says the oracle filter is off. The oracle UI would stay switched "
        "off forever."
    )
