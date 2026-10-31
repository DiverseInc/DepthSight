import asyncio
import logging
import sys
import os

sys.path.append(os.getcwd())  # Add current directory to path
import pandas as pd
from bot_module.compass_strategy import CompassStrategy

# NOTE: this file used to register CompassStrategy into the GLOBAL registry at
# import time:
#
#     from bot_module.strategy import STRATEGIES
#     STRATEGIES["CompassStrategy"] = CompassStrategy
#
# pytest imports every test module during COLLECTION, so that line ran in
# EVERY session -- it put CompassStrategy into the registry whether or not
# the registry should have contained it. It was invisible because the
# registry's client list is computed at import time and did not move with
# it. Once the registry can refuse a strategy that cannot actually trade,
# the two drifted apart and the invariant "the client's list is exactly the
# runtime registry" broke -- because of a line in a MANUAL script.
#
# A manual script has no business mutating global state at import time, so
# it uses its own mapping.
LOCAL_REGISTRY = {"CompassStrategy": CompassStrategy}

# Setup basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_compass")


async def test_compass_logic():
    print("\n--- Testing Compass Strategy Instantiation ---")
    if "CompassStrategy" not in LOCAL_REGISTRY:
        print("FAIL: CompassStrategy not in the local registry.")
        return

    strategy = LOCAL_REGISTRY["CompassStrategy"]()
    print(f"PASS: Strategy Instantiated. Enabled: {strategy.enabled}")
    print(f"Features: {strategy.feature_names}")

    print("\n--- Testing Aggregation Logic Matches ---")
    # Simulate DataConsumer._aggregate_depth behavior (as code reviewed)
    # Market Price 100.
    # Bid at 99.5 (-0.5% deviation) -> Bids [-1, -5] -> Bucket -1.
    # Bid at 96 (-4.0% deviation) -> Bucket -4.
    # Ask at 100.5 (+0.5% deviation) -> Bucket 1.

    depth_aggregated = {
        "bids": [
            {"percentage": -1, "notional": 1000.0},  # bids_1p = 1000
            {"percentage": -2, "notional": 500.0},
        ],
        "asks": [
            {"percentage": 1, "notional": 800.0},  # asks_1p = 800
            {"percentage": 2, "notional": 600.0},
        ],
    }

    # 30s AggTrades
    recent_trades = [
        {"p": 100.0, "q": 1.0, "m": False},  # Taker Buy (m=False), Notional 100
        {"p": 100.0, "q": 2.0, "m": True},  # Taker Sell (m=True), Notional 200
    ]
    # Tape Buy = 100, Tape Sell = 200

    # Kline
    kline = pd.DataFrame(
        [
            {
                "close": 100.0,
                "high": 101.0,
                "low": 99.0,
                "volume": 1000,
                "natr": 1.5,
                "relative_volume": 2.0,
            }
        ]
    )

    print("\n--- Testing Feature Adapter ---")
    feats = strategy.adapter.calculate_compass_features(
        kline, depth_aggregated, recent_trades
    )

    print("Features Calculated:", feats)

    # Validation
    # obi_1p = (1000 - 800) / (1000 + 800) = 200 / 1800 = 0.111...
    expected_obi = (1000 - 800) / (1000 + 800)
    print(f"OBI 1P: Expected={expected_obi:.4f}, Got={feats['obi_1p']:.4f}")

    # pressure_buy = tape_buy (100) / asks_1p (800) = 0.125
    print(f"Pressure Buy: Expected=0.125, Got={feats['pressure_buy']:.4f}")

    # pressure_sell = tape_sell (200) / bids_1p (1000) = 0.2
    print(f"Pressure Sell: Expected=0.2, Got={feats['pressure_sell']:.4f}")

    assert abs(feats["obi_1p"] - expected_obi) < 0.0001
    assert abs(feats["pressure_buy"] - 0.125) < 0.0001

    print("PASS: Feature Calculation Logic Verified.")

    print("\n--- Testing Standard Params ---")
    tp_mult = strategy._get_param("take_profit_atr_multiplier")
    print(f"TP Multiplier: {tp_mult} (Expected 7.5)")
    assert tp_mult == 7.5

    partial = strategy._get_param("partial_exits")
    print(f"Partials: {partial}")

    print("\n--- ALL TESTS PASSED ---")


if __name__ == "__main__":
    asyncio.run(test_compass_logic())
