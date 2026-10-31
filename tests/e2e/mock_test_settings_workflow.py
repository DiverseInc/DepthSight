# tests/e2e/test_settings_workflow.py
import pytest
from httpx import AsyncClient
from api import schemas


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_full_settings_workflow(authenticated_client: AsyncClient):
    """
    Verifies the full cycle of CRUD operations on the settings page.
    """
    client = authenticated_client

    # --- 1. Getting the initial configuration ---
    print("\n[E2E Settings Test] 1. Fetching initial config...")
    response_get1 = await client.get("/api/v1/config")
    assert response_get1.status_code == 200
    config1 = schemas.AppConfig.model_validate(response_get1.json()["data"])
    assert "BTCUSDT" in config1.data_sources["symbols"]
    assert "ETHUSDT" in config1.data_sources["symbols"]
    assert len(config1.api_keys) == 0

    # --- 2. Adding a new API key ---
    print("[E2E Settings Test] 2. Adding new API key...")
    api_key_payload = {
        "name": "My Testnet Key",
        "exchange": "binance_futures",
        "api_key": "test_api_key_1234567890",
        "api_secret": "test_api_secret_abcdefghij",
    }
    response_add_key = await client.post(
        "/api/v1/config/api-keys", json=api_key_payload
    )
    assert response_add_key.status_code == 201
    new_key = schemas.ApiKey.model_validate(response_add_key.json()["data"])
    assert new_key.name == "My Testnet Key"
    assert new_key.key_prefix.startswith("test_")

    # Verify that the key appeared in the config
    response_get2 = await client.get("/api/v1/config")
    config2 = schemas.AppConfig.model_validate(response_get2.json()["data"])
    assert len(config2.api_keys) == 1
    assert config2.api_keys[0].id == new_key.id

    # --- 3. Adding a new symbol ---
    print("[E2E Settings Test] 3. Adding new symbol...")
    response_add_symbol = await client.post(
        "/api/v1/config/datasources/symbols", json={"symbol": "SOLUSDT"}
    )
    assert response_add_symbol.status_code == 200
    updated_sources1 = response_add_symbol.json()["data"]
    assert "SOLUSDT" in updated_sources1["symbols"]

    # --- 4. Deleting the symbol ---
    print("[E2E Settings Test] 4. Deleting a symbol...")
    response_del_symbol = await client.delete(
        "/api/v1/config/datasources/symbols/ETHUSDT"
    )
    assert response_del_symbol.status_code == 200
    updated_sources2 = response_del_symbol.json()["data"]
    assert "ETHUSDT" not in updated_sources2["symbols"]
    assert "SOLUSDT" in updated_sources2["symbols"]

    # --- 5. Updating the Risk Management section ---
    print("[E2E Settings Test] 5. Updating risk management settings...")
    # CORRECTION (2026-10-31, round 22d): this used to send
    # ``maxDrawdown = 15.5``. An account is handed ``maxDrawdown = 10.0``
    # (MEASURED, ``api/crud.py`` ``create_user``), so 15.5 was a LOOSENING
    # of the account's own limit, and the Risk Settings route now refuses it
    # -- correctly: a client may tighten a risk limit and never loosen one.
    # 8.0 is a real tightening, so the script still exercises a successful
    # partial update.
    risk_update_payload = {
        "risk_management": {
            "maxDrawdown": 8.0,
            "maxConcurrentTrades": 7,
            "stopLossEnabled": False,
        }
    }
    response_update_risk = await client.put("/api/v1/config", json=risk_update_payload)
    assert response_update_risk.status_code == 200
    config3 = schemas.AppConfig.model_validate(response_update_risk.json()["data"])
    assert config3.risk_management.maxDrawdown == 8.0
    assert config3.risk_management.maxConcurrentTrades == 7
    assert config3.risk_management.stopLossEnabled is False

    # --- 6. Deleting the API key ---
    print(f"[E2E Settings Test] 6. Deleting API key ID: {new_key.id}...")
    response_del_key = await client.delete(f"/api/v1/config/api-keys/{new_key.id}")
    assert response_del_key.status_code == 204  # No Content

    # Verify that the key is actually deleted
    response_get4 = await client.get("/api/v1/config")
    config4 = schemas.AppConfig.model_validate(response_get4.json()["data"])
    assert len(config4.api_keys) == 0

    print("[E2E Settings Test] Workflow completed successfully.")
