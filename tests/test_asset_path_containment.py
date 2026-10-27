"""Tests that an asset name can never walk out of the data root.

`asset` arrives in the request (URL path for /asset-detail, JSON body for the
simulation routes), so it must not be able to reach a parquet file outside
DATA_STORAGE_PATH with `..` segments.

Every simulation route that loads an asset has to resolve its parquet path
through _resolve_asset_parquet_path, which fails closed with a 404.
"""

import pytest

from api import simulation_router

pytestmark = pytest.mark.asyncio

# Names that walk out of the data root. Every one of these escapes on POSIX
# and on Windows.
#
# NOTE: a four-dot component ("....") is deliberately NOT in this list. Windows
# strips trailing dots from a path component, so "...." collapses to the
# parent directory there, but on POSIX it is an ordinary directory name that
# stays INSIDE the data root. Asserting it is refused would fail on Linux.
ESCAPING_ASSETS = ["..", "../..", "a/../..", "BTCUSDT/../../.."]

# The URL path form of "..". httpx normalises a literal ".." path segment
# away, so the dot has to be percent-encoded to reach the handler.
ESCAPING_ASSET_URLS = ["%2E%2E"]

ASSET_DETAIL_BODY = {
    "strategy_json": {},
    "variants": [],
    "config": {},
}

# The simulation routes that take a list of assets in the request body.
SIMULATION_ROUTES = [
    ("/api/simulation/backtest", {"strategy_json": {}, "assets": []}),
    (
        "/api/simulation/inspector",
        {"strategy_json": {}, "assets": [], "variants": []},
    ),
    (
        "/api/simulation/inspector/stream",
        {"strategy_json": {}, "assets": [], "variants": []},
    ),
    (
        "/api/simulation/portfolio-full",
        {"strategy_json": {}, "assets": [], "variants": [], "config": {}},
    ),
]


def _body_for(route, assets):
    for path, template in SIMULATION_ROUTES:
        if path == route:
            body = dict(template)
            body["assets"] = assets
            return body
    raise AssertionError(f"unknown route {route}")


@pytest.mark.parametrize("asset", ESCAPING_ASSET_URLS)
async def test_asset_detail_refuses_an_escaping_asset_name(authenticated_client, asset):
    response = await authenticated_client.post(
        f"/api/simulation/asset-detail/{asset}", json=ASSET_DETAIL_BODY
    )
    assert response.status_code == 404, response.text
    # Proves the handler produced this, not the router (which answers
    # {"detail": "Not Found"}).
    assert "Asset data not found" in response.text


@pytest.mark.parametrize("route", [path for path, _ in SIMULATION_ROUTES])
@pytest.mark.parametrize("asset", ESCAPING_ASSETS)
async def test_simulation_routes_refuse_an_escaping_asset_name(
    authenticated_client, route, asset
):
    """Every simulation route that loads an asset must fail closed."""
    response = await authenticated_client.post(route, json=_body_for(route, [asset]))
    assert response.status_code == 404, response.text
    assert "Asset data not found" in response.text


@pytest.mark.parametrize("route", [path for path, _ in SIMULATION_ROUTES])
async def test_simulation_routes_still_accept_a_normal_asset_name(
    authenticated_client, route
):
    """A normal asset name is not refused by the containment check."""
    response = await authenticated_client.post(
        route, json=_body_for(route, ["BTCUSDT"])
    )
    # The asset simply has no data in the test environment, so the route
    # answers 404 for "no such parquet" - but NOT the containment message.
    assert "Asset data not found" not in response.text


async def test_resolve_asset_parquet_path_refuses_to_escape_the_data_root(
    monkeypatch, tmp_path
):
    """`asset` comes from the request, so it must not be able to walk out."""
    data_root = tmp_path / "data_root"
    data_root.mkdir()
    monkeypatch.setattr(simulation_router, "DATA_STORAGE_PATH", data_root)

    inside = simulation_router._resolve_asset_parquet_path("BTCUSDT")
    assert inside == (data_root / "BTCUSDT" / "kline_1m.parquet")

    for escaping in ESCAPING_ASSETS:
        with pytest.raises(Exception) as exc_info:
            simulation_router._resolve_asset_parquet_path(escaping)
        assert getattr(exc_info.value, "status_code", None) == 404


async def test_asset_detail_cannot_read_a_file_outside_the_data_root(
    authenticated_client, monkeypatch, tmp_path
):
    """A parquet file outside the data root must not be readable via `..`."""
    import pandas as pd

    data_root = tmp_path / "data_root"
    data_root.mkdir()
    monkeypatch.setattr(simulation_router, "DATA_STORAGE_PATH", data_root)

    # This file sits OUTSIDE the data root (it is the parent directory).
    pd.DataFrame(
        {
            "time": [1],
            "open": [1.0],
            "high": [1.0],
            "low": [1.0],
            "close": [1.0],
            "volume": [1.0],
        }
    ).to_parquet(tmp_path / "kline_1m.parquet")

    response = await authenticated_client.post(
        "/api/simulation/asset-detail/%2E%2E", json=ASSET_DETAIL_BODY
    )
    assert response.status_code == 404, response.text
    assert "klines" not in response.text
