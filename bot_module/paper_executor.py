# bot_module/paper_executor.py

import asyncio
import logging
import uuid
from typing import Dict, Any, Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta, timezone

from bot_module.runtime_dependencies import crud
from .data_consumer import DataConsumer
from .execution_simulator import (
    OrderExecutionResult,
    FillType,
)
from .strategy import SignalDirection
from bot_module.config import (
    BACKTEST_COMMISSION_PCT,
    BACKTEST_SLIPPAGE_PCT,
    PAPER_ASSUMED_LEVERAGE,
    PAPER_FUNDING_INTERVAL_HOURS,
    PAPER_FUNDING_RATE_8H,
    PAPER_LIQUIDATION_MMR,
    PAPER_SIMULATE_FUNDING,
    PAPER_SIMULATE_LIQUIDATION,
)

logger = logging.getLogger(__name__)


class PaperTradingExecutor:
    def __init__(
        self,
        user_id: int,
        db_session: AsyncSession,
        data_consumer: DataConsumer,
        redis_client=None,
    ):
        self.user_id = user_id
        self.db = db_session
        self.data_consumer = data_consumer
        self.redis_client = redis_client  # For recording equity history
        self._open_orders: Dict[str, Dict[str, Any]] = {}
        self._positions: Dict[str, Dict[str, Any]] = {}
        self.market_type = "futures_usdtm"
        self._db_lock = asyncio.Lock()
        self.controller = None  # Will be set by the controller after initialization
        self._equity_initialized = False  # Flag for initial point initialization
        logger.info(f"PaperTradingExecutor initialized for user_id: {self.user_id}")

    async def close(self):
        logger.info("Closing PaperTradingExecutor.")
        pass

    async def get_account_balance(self) -> Optional[Dict[str, Dict[str, str]]]:
        logger.debug(f"Getting paper wallet balance for user_id: {self.user_id}")
        try:
            wallet_assets = await crud.get_paper_wallet(self.db, user_id=self.user_id)

            if not wallet_assets:
                logger.info(
                    f"Paper wallet not found for user {self.user_id}. Initializing..."
                )
                wallet_assets = await crud.init_or_reset_paper_wallet(
                    self.db, user_id=self.user_id
                )

            balances = {}
            for asset in wallet_assets:
                balances[asset.asset] = {"free": str(asset.balance), "locked": "0.0"}
            logger.debug(f"Paper wallet balance: {balances}")
            return balances
        except Exception as e:
            logger.error(
                f"Error getting paper account balance for user {self.user_id}: {e}",
                exc_info=True,
            )
            return None

    async def place_order(
        self, symbol: str, side: str, order_type: str, **kwargs
    ) -> Dict[str, Any]:
        client_order_id = kwargs.get("newClientOrderId", str(uuid.uuid4()))
        log_prefix = f"[PaperPlaceOrder:{client_order_id}]"
        logger.info(
            f"{log_prefix} Simulating place order for {symbol}: {side} {order_type} with params {kwargs}"
        )

        if order_type.upper() == "MARKET":
            quantity = float(kwargs.get("quantity"))
            direction = (
                SignalDirection.LONG if side.upper() == "BUY" else SignalDirection.SHORT
            )

            avg_fill_price = None
            filled_quantity = 0

            enriched_depth = await self.data_consumer.get_latest_depth(symbol)
            aggregated_depth = None
            current_price_for_fallback = None

            if enriched_depth:
                aggregated_depth = enriched_depth.get("aggregated_depth")
                if enriched_depth.get("full_l2_depth") and enriched_depth[
                    "full_l2_depth"
                ].get("bids"):
                    best_bid = float(enriched_depth["full_l2_depth"]["bids"][0][0])
                    best_ask = float(enriched_depth["full_l2_depth"]["asks"][0][0])
                    current_price_for_fallback = (best_bid + best_ask) / 2

            if aggregated_depth and (
                aggregated_depth.get("bids") or aggregated_depth.get("asks")
            ):
                logger.info(
                    f"{log_prefix} Simulating execution with aggregated order book."
                )
                order_book_side = (
                    aggregated_depth.get("asks")
                    if direction == SignalDirection.LONG
                    else aggregated_depth.get("bids")
                )
                remaining_qty = quantity
                total_cost = 0.0

                for bucket in order_book_side:
                    if remaining_qty <= 0:
                        break
                    bucket_price = bucket.get("avg_price", 0.0)
                    bucket_notional = bucket.get("notional", 0.0)
                    if bucket_price <= 0 or bucket_notional <= 0:
                        continue
                    bucket_qty_available = bucket_notional / bucket_price
                    qty_to_fill = min(remaining_qty, bucket_qty_available)
                    total_cost += qty_to_fill * bucket_price
                    filled_quantity += qty_to_fill
                    remaining_qty -= qty_to_fill

                if filled_quantity > 0:
                    avg_fill_price = total_cost / filled_quantity
                if remaining_qty > 0:
                    logger.warning(
                        f"{log_prefix} Order partially filled. Requested: {quantity}, Filled: {filled_quantity}. Remaining: {remaining_qty}"
                    )

            if avg_fill_price is None:
                logger.warning(
                    f"{log_prefix} Aggregated depth not available or empty. Falling back to simple slippage simulation."
                )

                current_price_for_fallback = await self.data_consumer.get_latest_price(
                    symbol
                )

                if current_price_for_fallback is None:
                    msg = f"Could not get current price for {symbol} to simulate market order."
                    logger.error(f"{log_prefix} {msg}")
                    return {"error": True, "code": -1001, "msg": msg}

                slippage = current_price_for_fallback * BACKTEST_SLIPPAGE_PCT
                avg_fill_price = (
                    current_price_for_fallback + slippage
                    if direction == SignalDirection.LONG
                    else current_price_for_fallback - slippage
                )
                filled_quantity = quantity

            commission = avg_fill_price * filled_quantity * BACKTEST_COMMISSION_PCT

            # Use KLINE_SLIPPAGE as it's the most common fallback type
            sim_result = OrderExecutionResult(
                avg_fill_price=avg_fill_price,
                filled_quantity=filled_quantity,
                actual_commission_paid=commission,
                fill_type=FillType.KLINE_SLIPPAGE,
            )
            if sim_result.filled_quantity > 0:
                async with self._db_lock:
                    try:
                        realized_pnl = 0.0
                        asset_to_update = "USDT"

                        # Check if this trade is a closure or a reduction of an existing position
                        if symbol in self._positions:
                            old_pos = self._positions[symbol]
                            old_qty = old_pos["quantity"]

                            is_long_position = old_qty > 0
                            is_reducing_trade = (
                                is_long_position and direction == SignalDirection.SHORT
                            ) or (
                                not is_long_position
                                and direction == SignalDirection.LONG
                            )

                            if is_reducing_trade:
                                # Calculate PnL for the closing part
                                qty_being_closed = min(
                                    abs(old_qty), sim_result.filled_quantity
                                )
                                entry_price = old_pos["avg_entry_price"]
                                exit_price = sim_result.avg_fill_price

                                if is_long_position:
                                    realized_pnl = (
                                        exit_price - entry_price
                                    ) * qty_being_closed
                                else:  # Short position
                                    realized_pnl = (
                                        entry_price - exit_price
                                    ) * qty_being_closed

                                logger.info(
                                    f"{log_prefix} Closing trade detected. Realized PnL for this fill: {realized_pnl:.4f}"
                                )

                        # For futures, the balance only changes by PnL (if any) and commission.
                        # The full contract value is NOT debited and NOT credited.
                        total_balance_change = (
                            realized_pnl - sim_result.actual_commission_paid
                        )

                        await crud.update_paper_wallet_balance(
                            self.db, self.user_id, asset_to_update, total_balance_change
                        )

                        # Recording the equity history point in Redis
                        await self._record_equity_point()

                        # Position update logic (remains the same)
                        position_change = (
                            sim_result.filled_quantity
                            if direction == SignalDirection.LONG
                            else -sim_result.filled_quantity
                        )

                        position_leverage = self._leverage_from_signal_details(
                            kwargs.get("signal_details")
                        )
                        now_ts = datetime.now(timezone.utc)

                        if symbol not in self._positions:
                            self._positions[symbol] = {
                                "quantity": position_change,
                                "avg_entry_price": sim_result.avg_fill_price,
                                "entry_timestamp": now_ts,  # Save entry time
                                "leverage": position_leverage,
                                # Funding settles on fixed UTC boundaries. Anchor to the
                                # entry time; _funding_periods_since() only counts
                                # boundaries strictly after it, so a position opened at
                                # 07:59 is not billed a full period for 1 minute of
                                # exposure.
                                "last_funding_timestamp": now_ts,
                                # Carried so a forced close (liquidation) records a
                                # trade that groups with the normal entry trade.
                                "strategy_config_id": kwargs.get("strategy_config_id"),
                                "entry_client_order_id": kwargs.get(
                                    "entry_client_order_id"
                                ),
                            }
                        else:
                            old_pos = self._positions[symbol]
                            old_qty = old_pos["quantity"]
                            old_avg_price = old_pos["avg_entry_price"]
                            new_qty = old_qty + position_change

                            if abs(new_qty) > 1e-9:
                                if (old_qty > 0 and position_change > 0) or (
                                    old_qty < 0 and position_change < 0
                                ):  # Position increase
                                    # Recalculate the average entry price
                                    new_avg_price = (
                                        (old_avg_price * old_qty)
                                        + (sim_result.avg_fill_price * position_change)
                                    ) / new_qty
                                else:  # Position reduction
                                    new_avg_price = (
                                        old_avg_price  # Entry price does not change
                                    )

                                # Carry entry time and leverage forward. The previous
                                # code rebuilt this dict with only quantity and price,
                                # silently dropping entry_timestamp on every scale-in
                                # and reduction — which reset the position's age and
                                # would also reset funding accounting.
                                self._positions[symbol] = {
                                    "quantity": new_qty,
                                    "avg_entry_price": new_avg_price,
                                    "entry_timestamp": old_pos.get(
                                        "entry_timestamp", now_ts
                                    ),
                                    "leverage": old_pos.get(
                                        "leverage", position_leverage
                                    ),
                                    "last_funding_timestamp": old_pos.get(
                                        "last_funding_timestamp", now_ts
                                    ),
                                    "strategy_config_id": old_pos.get(
                                        "strategy_config_id"
                                    )
                                    or kwargs.get("strategy_config_id"),
                                    "entry_client_order_id": old_pos.get(
                                        "entry_client_order_id"
                                    )
                                    or kwargs.get("entry_client_order_id"),
                                }
                            else:  # Position is fully closed
                                del self._positions[symbol]

                        # Logic for recording the trade in the DB
                        # Determining the exit type (for grouping trades)
                        # Using the passed exit_type or determining by PnL
                        exit_type = kwargs.get("exit_type")
                        if not exit_type:
                            exit_type = "ENTRY" if realized_pnl == 0 else "EXIT"

                        is_final_exit = (
                            symbol not in self._positions
                        )  # If the position is deleted, it means a final exit

                        # Determining the correct entry_price and exit_price
                        # If this is a position closure - take entry_price from the old position
                        actual_entry_price = sim_result.avg_fill_price
                        actual_exit_price = sim_result.avg_fill_price
                        actual_entry_timestamp = datetime.now(
                            timezone.utc
                        )  # Default for entry orders

                        if realized_pnl != 0 and "old_pos" in dir() and old_pos:
                            # This is a position closure - entry_price from the old position, exit_price is current
                            actual_entry_price = old_pos.get(
                                "avg_entry_price", sim_result.avg_fill_price
                            )
                            actual_exit_price = sim_result.avg_fill_price
                            actual_entry_timestamp = old_pos.get(
                                "entry_timestamp", datetime.now(timezone.utc)
                            )

                        trade_data = {
                            "trade_uuid": client_order_id,
                            "timestamp_close": datetime.now(timezone.utc),
                            "timestamp_signal": actual_entry_timestamp,
                            "symbol": symbol,
                            "strategy_config_id": kwargs.get("strategy_config_id"),
                            "direction": side,
                            "entry_price": actual_entry_price,
                            "exit_price": actual_exit_price,
                            "pnl": realized_pnl,
                            "commission": sim_result.actual_commission_paid,
                            "exit_reason": "PAPER_TRADE_MARKET_EXECUTION",
                            "quantity": sim_result.filled_quantity,
                            # New fields for grouping
                            "position_entry_id": kwargs.get(
                                "entry_client_order_id"
                            ),  # Entry ID for grouping
                            "exit_type": exit_type,
                            "is_final_exit": is_final_exit,
                            # Signal details with decision trace for analytics
                            "signal_details_json": kwargs.get("signal_details"),
                        }

                        from tasks import process_live_trade_analytics_task

                        new_db_trade = await crud.create_trade(
                            self.db,
                            user_id=self.user_id,
                            trade_data=trade_data,
                            trade_mode="PAPER",
                        )
                        await self.db.commit()
                        await self.db.refresh(new_db_trade)
                        logger.info(
                            f"{log_prefix} Trade {new_db_trade.id} successfully saved and committed."
                        )

                        try:
                            process_live_trade_analytics_task.delay(
                                trade_id=new_db_trade.id, user_id=self.user_id
                            )
                            logger.info(
                                f"{log_prefix} Analytical task started for trade_id: {new_db_trade.id}"
                            )
                        except Exception as e_celery:
                            logger.error(
                                f"{log_prefix} Error starting analytical task for trade_id={new_db_trade.id}: {e_celery}",
                                exc_info=True,
                            )

                        # Returning the response (slightly modified for correctness)
                        return {
                            "symbol": symbol,
                            "orderId": int(uuid.uuid4().int & (1 << 32) - 1),
                            "clientOrderId": client_order_id,
                            "transactTime": int(
                                datetime.now(timezone.utc).timestamp() * 1000
                            ),
                            "price": "0",
                            "origQty": str(quantity),
                            "executedQty": str(sim_result.filled_quantity),
                            "cummulativeQuoteQty": str(
                                sim_result.avg_fill_price * sim_result.filled_quantity
                            ),
                            "status": "FILLED",
                            "timeInForce": "GTC",
                            "type": "MARKET",
                            "side": side,
                            "fills": [
                                {
                                    "price": str(sim_result.avg_fill_price),
                                    "qty": str(sim_result.filled_quantity),
                                    "commission": str(
                                        sim_result.actual_commission_paid
                                    ),
                                    "commissionAsset": "USDT",
                                }
                            ],
                        }
                    except Exception as e:
                        msg = (
                            f"Failed to update paper wallet or create trade record: {e}"
                        )
                        logger.error(f"{log_prefix} {msg}", exc_info=True)
                        await self.db.rollback()
                        return {"error": True, "code": -1002, "msg": msg}
            else:
                return {
                    "error": True,
                    "code": -1003,
                    "msg": "Failed to fill market order in simulation.",
                }

        elif order_type.upper() == "LIMIT":
            order = {
                "symbol": symbol,
                "orderId": int(uuid.uuid4().int & (1 << 32) - 1),
                "clientOrderId": client_order_id,
                "price": str(kwargs.get("price")),
                "origQty": str(kwargs.get("quantity")),
                "executedQty": "0.0",
                "status": "NEW",
                "timeInForce": kwargs.get("timeInForce"),
                "type": "LIMIT",
                "side": side,
                "time": int(datetime.now(timezone.utc).timestamp() * 1000),
                "strategy_config_id": kwargs.get("strategy_config_id"),
                "signal_details": kwargs.get("signal_details"),  # Save for analytics
            }
            self._open_orders[client_order_id] = order
            logger.info(f"{log_prefix} Stored LIMIT order {client_order_id} in memory.")
            return order
        elif order_type.upper() == "STOP_MARKET":
            # Get prices and convert to string only if they exist
            price_val = kwargs.get("price")
            stop_price_val = kwargs.get("stopPrice")

            order = {
                "symbol": symbol,
                "orderId": int(uuid.uuid4().int & (1 << 32) - 1),
                "clientOrderId": client_order_id,
                "price": str(price_val) if price_val is not None else None,
                "stopPrice": str(stop_price_val)
                if stop_price_val is not None
                else None,
                "origQty": str(kwargs.get("quantity")),
                "executedQty": "0.0",
                "status": "NEW",
                "timeInForce": kwargs.get("timeInForce", "GTC"),
                "type": "STOP_MARKET",
                "side": side,
                "time": int(datetime.now(timezone.utc).timestamp() * 1000),
                "strategy_config_id": kwargs.get("strategy_config_id"),
                "signal_details": kwargs.get("signal_details"),  # Save for analytics
            }
            self._open_orders[client_order_id] = order
            logger.info(
                f"{log_prefix} Stored STOP_MARKET order {client_order_id} in memory."
            )
            return order
        else:
            logger.info(
                f"{log_prefix} Order type {order_type} not fully supported in paper trading yet. Accepting as new."
            )
            return {
                "symbol": symbol,
                "orderId": int(uuid.uuid4().int & (1 << 32) - 1),
                "clientOrderId": client_order_id,
                "status": "NEW",
                "type": order_type,
                "side": side,
            }

    async def cancel_order(
        self,
        symbol: str,
        orderId: Optional[int] = None,
        origClientOrderId: Optional[str] = None,
    ) -> Dict[str, Any]:
        log_prefix = "[PaperCancelOrder]"
        order_to_cancel_id = origClientOrderId

        if order_to_cancel_id and order_to_cancel_id in self._open_orders:
            cancelled_order = self._open_orders.pop(order_to_cancel_id)
            cancelled_order["status"] = "CANCELED"
            logger.info(
                f"{log_prefix} Canceled order {order_to_cancel_id} for symbol {symbol}."
            )
            return cancelled_order
        else:
            msg = f"Order with clientOrderId {order_to_cancel_id} not found in paper trading open orders."
            logger.warning(f"{log_prefix} {msg}")
            return {"error": True, "code": -2011, "msg": "Unknown order sent."}

    async def get_open_orders(self, symbol: Optional[str] = None) -> list:
        if symbol:
            return [
                order
                for order in self._open_orders.values()
                if order["symbol"] == symbol
            ]
        return list(self._open_orders.values())

    async def get_open_positions(self) -> List[Dict[str, Any]]:
        positions = []
        for symbol, pos_data in self._positions.items():
            quantity = pos_data.get("quantity", 0.0)
            avg_entry_price = pos_data.get("avg_entry_price", 0.0)

            if abs(quantity) > 1e-9:
                current_price = await self.data_consumer.get_latest_price(symbol)

                pnl = 0.0
                if avg_entry_price > 0 and current_price:
                    if quantity > 0:  # LONG
                        pnl = (current_price - avg_entry_price) * quantity
                    else:  # SHORT
                        pnl = (avg_entry_price - current_price) * abs(quantity)

                # Report the REAL liquidation price. This used to be hardcoded "0",
                # which told the user (and the UI) that a leveraged position had no
                # liquidation risk when it very much did.
                liq_price = self._position_liquidation_price(symbol, pos_data)

                positions.append(
                    {
                        "symbol": symbol,
                        "positionAmt": str(quantity),
                        "entryPrice": str(avg_entry_price),
                        "markPrice": str(current_price) if current_price else "0",
                        "unRealizedProfit": str(pnl),
                        "liquidationPrice": (
                            str(liq_price) if liq_price else "0"
                        ),
                        "leverage": str(pos_data.get("leverage") or 1.0),
                    }
                )
        return positions

    async def get_ticker_price(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Simulates getting the ticker price for Paper Trading.
        Retrieves the latest price from DataConsumer.
        """
        log_prefix = f"[PaperTickerPrice:{symbol}]"

        last_price = await self.data_consumer.get_latest_price(symbol)

        if last_price is not None:
            logger.debug(
                f"{log_prefix} Found latest price in DataConsumer: {last_price}"
            )
            # Returning a dictionary in a format compatible with BinanceExecutor
            return {"symbol": symbol, "price": str(last_price)}
        else:
            logger.warning(
                f"{log_prefix} Could not find latest price for {symbol} in DataConsumer."
            )
            return None

    # ------------------------------------------------------------------
    # Realism: leverage, liquidation and funding
    # ------------------------------------------------------------------
    @staticmethod
    def _leverage_from_signal_details(signal_details: Any) -> float:
        """
        Read the leverage a strategy declared, using the SAME key list as
        TradingController._leverage_for_position so paper and live agree on
        what "20x" means. Falls back to PAPER_ASSUMED_LEVERAGE.

        Note: the bot never calls set_leverage() anywhere, so on the live path
        the real leverage is the OKX account default and is invisible to us.
        PAPER_ASSUMED_LEVERAGE is the honest acknowledgement of that gap.
        """
        details = signal_details if isinstance(signal_details, dict) else {}
        for key in ("leverage", "leverage_x", "leverageX", "leverage_multiplier"):
            value = details.get(key)
            if value is None:
                continue
            try:
                lev = float(value)
            except (TypeError, ValueError):
                continue
            if lev > 0:
                return lev
        return float(PAPER_ASSUMED_LEVERAGE)

    @staticmethod
    def liquidation_price_for(
        entry_price: float, quantity: float, leverage: float, mmr: float
    ) -> Optional[float]:
        """
        Isolated USDT-margined liquidation price for a single position.

        Liquidation happens when equity == maintenance margin, with
        margin = E*q/lev.

        LONG  (pnl = (P - E)*q):
            E/lev + P - E = P*mmr
            P*(1 - mmr)   = E*(1 - 1/lev)
            P = E*(1 - 1/lev) / (1 - mmr)

        SHORT (pnl = (E - P)*q):
            E/lev + E - P = P*mmr
            P*(1 + mmr)   = E*(1 + 1/lev)
            P = E*(1 + 1/lev) / (1 + mmr)

        Note the denominators DIFFER: for a long the notional GROWS as price
        moves against you, so the maintenance requirement grows too (1 - mmr);
        for a short the notional SHRINKS, so it does not (1 + mmr). Using
        (1 - mmr) for both is the classic slip and it makes a short survive
        ~1% longer than it really would — i.e. it biases the demo optimistic,
        which is exactly the failure this whole change exists to remove.

        Returns None at leverage <= 1, where liquidation is not reachable at a
        sane price — the same "no liquidation price" the exchange reports as 0.
        Modelling one anyway would invent a liquidation the user would never
        actually experience.
        """
        if entry_price is None or entry_price <= 0:
            return None
        if quantity is None or abs(quantity) <= 1e-9:
            return None
        if leverage is None or leverage <= 1.0:
            return None
        mmr = float(mmr if mmr is not None else 0.0)
        if mmr < 0.0 or mmr >= 1.0:
            return None
        try:
            inv_lev = 1.0 / float(leverage)
        except (TypeError, ValueError, ZeroDivisionError):
            return None

        if quantity > 0:  # LONG
            denom = 1.0 - mmr
            if denom <= 0:
                return None
            return entry_price * (1.0 - inv_lev) / denom
        # SHORT
        return entry_price * (1.0 + inv_lev) / (1.0 + mmr)

    @staticmethod
    def _funding_periods_since(
        t_start: Optional[datetime], t_end: datetime, interval_hours: int = 8
    ) -> int:
        """
        Count funding boundaries strictly after t_start and up to t_end.

        Mirrors count_funding_periods() in the backtesters so a paper run bills
        the same periods a backtest would. Boundaries are UTC hours where
        hour % interval_hours == 0.
        """
        if t_start is None or t_end is None:
            return 0
        if t_start.tzinfo is None:
            t_start = t_start.replace(tzinfo=timezone.utc)
        if t_end.tzinfo is None:
            t_end = t_end.replace(tzinfo=timezone.utc)
        t_start = t_start.astimezone(timezone.utc)
        t_end = t_end.astimezone(timezone.utc)
        if t_end <= t_start:
            return 0
        periods = 0
        cursor = t_start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        while cursor <= t_end:
            if cursor.hour % interval_hours == 0 and cursor > t_start:
                periods += 1
            cursor += timedelta(hours=1)
        return periods

    def _position_liquidation_price(self, symbol: str, pos: Dict[str, Any]) -> Optional[float]:
        return self.liquidation_price_for(
            pos.get("avg_entry_price", 0.0),
            pos.get("quantity", 0.0),
            pos.get("leverage") or 0.0,
            PAPER_LIQUIDATION_MMR,
        )

    async def check_liquidations(self) -> None:
        """
        Liquidate paper positions whose mark price has crossed the liquidation
        price. Runs on every tick via check_open_orders().

        Before this existed, paper positions reported liquidationPrice "0" and
        were NEVER liquidated, so a strategy that would be blown out on OKX
        kept running and booked a profit. That is the single most misleading
        thing about the old demo.
        """
        if not PAPER_SIMULATE_LIQUIDATION:
            return
        if not self._positions:
            return

        for symbol in list(self._positions.keys()):
            pos = self._positions.get(symbol)
            if not pos:
                continue
            qty = pos.get("quantity", 0.0)
            if abs(qty) <= 1e-9:
                continue
            liq_price = self._position_liquidation_price(symbol, pos)
            if liq_price is None:
                continue
            try:
                mark = await self.data_consumer.get_latest_price(symbol)
            except Exception as e:
                logger.debug(
                    f"[PaperLiquidation:{symbol}] Could not read mark price: {e}"
                )
                continue
            if not mark or mark <= 0:
                continue
            mark = float(mark)

            is_liquidated = (qty > 0 and mark <= liq_price) or (
                qty < 0 and mark >= liq_price
            )
            if not is_liquidated:
                continue

            entry = pos.get("avg_entry_price", 0.0)
            logger.critical(
                f"[PaperLiquidation:{symbol}] LIQUIDATION — mark={mark:.8f} crossed "
                f"liq={liq_price:.8f} (entry={entry:.8f}, qty={qty}, "
                f"lev={pos.get('leverage')}). Closing at bankruptcy price."
            )
            try:
                await self._close_position_at(
                    symbol,
                    exit_price=liq_price,
                    reason="PAPER_LIQUIDATION",
                    exit_type="LIQUIDATION",
                )
            except Exception as e:
                logger.error(
                    f"[PaperLiquidation:{symbol}] Failed to process liquidation: {e}",
                    exc_info=True,
                )

    async def check_funding(self) -> None:
        """
        Charge funding on open paper positions for every settlement boundary
        crossed since the last charge. Longs pay, shorts receive — the same
        sign convention as calculate_position_funding_pnl() in the backtesters.
        """
        if not PAPER_SIMULATE_FUNDING:
            return
        if not self._positions:
            return
        rate = float(PAPER_FUNDING_RATE_8H or 0.0)
        if rate == 0.0:
            return

        now = datetime.now(timezone.utc)
        total_delta = 0.0
        charged: List[str] = []

        for symbol in list(self._positions.keys()):
            pos = self._positions.get(symbol)
            if not pos:
                continue
            qty = pos.get("quantity", 0.0)
            if abs(qty) <= 1e-9:
                continue
            entry = pos.get("avg_entry_price", 0.0) or 0.0
            if entry <= 0:
                continue

            since = pos.get("last_funding_timestamp") or pos.get("entry_timestamp")
            periods = self._funding_periods_since(
                since, now, PAPER_FUNDING_INTERVAL_HOURS
            )
            if periods <= 0:
                continue

            notional = abs(qty) * entry
            # Same convention as the backtesters: longs pay (negative), shorts
            # receive (positive).
            multiplier = 1.0 if qty < 0 else -1.0
            funding_pnl = multiplier * periods * notional * rate
            total_delta += funding_pnl
            charged.append(f"{symbol} {periods}p {funding_pnl:+.4f}")

            # Advance the anchor so the next tick does not re-bill the same
            # periods. Advance to the last boundary we just billed.
            pos["last_funding_timestamp"] = self._last_funding_boundary_before(
                now, PAPER_FUNDING_INTERVAL_HOURS
            )

        if not charged:
            return

        async with self._db_lock:
            try:
                await crud.update_paper_wallet_balance(
                    self.db, self.user_id, "USDT", total_delta
                )
                await self.db.commit()
            except Exception as e:
                logger.error(
                    f"[PaperFunding] Failed to apply funding {total_delta:+.4f}: {e}",
                    exc_info=True,
                )
                return

        logger.info(
            f"[PaperFunding] user={self.user_id} applied {total_delta:+.4f} USDT "
            f"({rate * 100:.4f}% per {PAPER_FUNDING_INTERVAL_HOURS}h): "
            + ", ".join(charged)
        )
        try:
            await self._record_equity_point()
        except Exception:
            pass

    @staticmethod
    def _last_funding_boundary_before(
        moment: datetime, interval_hours: int = 8
    ) -> datetime:
        """The most recent funding boundary at or before `moment` (UTC)."""
        m = moment.astimezone(timezone.utc)
        boundary = m.replace(minute=0, second=0, microsecond=0)
        while boundary.hour % interval_hours != 0:
            boundary -= timedelta(hours=1)
        return boundary

    async def _close_position_at(
        self,
        symbol: str,
        exit_price: float,
        reason: str,
        exit_type: str,
    ) -> None:
        """
        Close a whole paper position at a given price, applying the same
        balance/trade-recording side effects as a normal market close. Shared
        by liquidation and (future) other forced closes.
        """
        pos = self._positions.get(symbol)
        if not pos:
            return
        qty = pos.get("quantity", 0.0)
        entry = pos.get("avg_entry_price", 0.0)
        if abs(qty) <= 1e-9 or entry <= 0:
            return

        if qty > 0:  # LONG
            realized_pnl = (exit_price - entry) * abs(qty)
        else:  # SHORT
            realized_pnl = (entry - exit_price) * abs(qty)

        commission = exit_price * abs(qty) * BACKTEST_COMMISSION_PCT
        balance_change = realized_pnl - commission

        async with self._db_lock:
            try:
                await crud.update_paper_wallet_balance(
                    self.db, self.user_id, "USDT", balance_change
                )
                await self.db.commit()
            except Exception as e:
                logger.error(
                    f"[PaperClose:{symbol}] Wallet update failed: {e}", exc_info=True
                )
                return

            try:
                trade_data = {
                    "timestamp_close": datetime.now(timezone.utc),
                    "timestamp_signal": pos.get("entry_timestamp")
                    or datetime.now(timezone.utc),
                    "symbol": symbol,
                    "strategy_config_id": pos.get("strategy_config_id"),
                    "direction": "BUY" if qty > 0 else "SELL",
                    "entry_price": entry,
                    "exit_price": exit_price,
                    "pnl": realized_pnl,
                    "commission": commission,
                    "exit_reason": reason,
                    "quantity": abs(qty),
                    "exit_type": exit_type,
                    "is_final_exit": True,
                    "position_entry_id": pos.get("entry_client_order_id"),
                }
                new_trade = await crud.create_trade(
                    self.db,
                    user_id=self.user_id,
                    trade_data=trade_data,
                    trade_mode="PAPER",
                )
                await self.db.commit()
                try:
                    await self.db.refresh(new_trade)
                except Exception:
                    pass
                logger.critical(
                    f"[PaperClose:{symbol}] {reason} trade {getattr(new_trade, 'id', '?')} "
                    f"saved: pnl={realized_pnl:.4f} commission={commission:.4f} "
                    f"balance_change={balance_change:.4f}"
                )
            except Exception as e:
                logger.error(
                    f"[PaperClose:{symbol}] Failed to record {reason} trade: {e}",
                    exc_info=True,
                )

        del self._positions[symbol]

        try:
            await self._record_equity_point()
        except Exception:
            pass

        # Keep the controller's LivePosition mirror in sync so the dashboard
        # does not keep showing a position that no longer exists.
        try:
            await self._notify_controller_about_fill(
                symbol, exit_type, "SELL" if qty > 0 else "BUY", abs(qty), exit_price
            )
        except Exception as e:
            logger.debug(
                f"[PaperClose:{symbol}] Controller notify failed (non-fatal): {e}"
            )

    async def check_open_orders(self):
        """
        Per-tick maintenance for paper positions: liquidation and funding are
        checked FIRST and unconditionally (a position can be open with no SL/TP
        orders resting), then resting SL/TP orders are simulated.
        """
        await self.check_liquidations()
        await self.check_funding()

        if not self._open_orders:
            logger.debug("[PaperOrderCheck] No open orders to check.")
            return

        logger.info(
            f"[PaperOrderCheck] Checking {len(self._open_orders)} open orders..."
        )

        # Get current prices for all symbols that have open orders
        symbols_with_orders = {order["symbol"] for order in self._open_orders.values()}
        price_tasks = {
            symbol: self.data_consumer.get_latest_price(symbol)
            for symbol in symbols_with_orders
        }

        # Using gather for parallel price retrieval
        price_results = await asyncio.gather(*price_tasks.values())
        current_prices = dict(zip(symbols_with_orders, price_results))

        logger.debug(f"[PaperOrderCheck] Current prices: {current_prices}")

        executed_order_ids = []

        # Iterate over a copy so the dictionary can be modified inside the loop
        for order_id, order in list(self._open_orders.items()):
            symbol = order["symbol"]
            current_price = current_prices.get(symbol)
            if current_price is None:
                logger.warning(
                    f"[PaperOrderCheck] Could not get price for {symbol}, skipping order {order_id}"
                )
                continue  # Failed to get the price for this symbol, skipping

            price_str_from_order = order.get("price") or order.get("stopPrice")

            # Check that the price is not None or the string 'None' before conversion
            if price_str_from_order is None or price_str_from_order == "None":
                order_price = 0.0
            else:
                try:
                    order_price = float(price_str_from_order)
                except (ValueError, TypeError):
                    logger.warning(
                        f"[PaperOrderCheck] Could not parse price '{price_str_from_order}' for order {order_id}. Skipping."
                    )
                    continue

            order_type = order.get("type", "").upper()
            order_side = order.get("side", "").upper()

            logger.debug(
                f"[PaperOrderCheck] Order {order_id}: Type={order_type}, Side={order_side}, "
                f"OrderPrice={order_price}, CurrentPrice={current_price}, Symbol={symbol}"
            )

            # If the price is invalid, skip the order
            if order_price <= 0:
                logger.warning(
                    f"[PaperOrderCheck] Invalid order price {order_price} for order {order_id}, skipping"
                )
                continue
            should_execute = False

            # Check for different order types

            # 1. Logic for LIMIT orders (Take-profits)
            if order_type == "LIMIT":
                if (order_side == "BUY" and current_price <= order_price) or (
                    order_side == "SELL" and current_price >= order_price
                ):
                    should_execute = True
                    logger.info(
                        f"[PaperOrderCheck] LIMIT order {order_id} triggered: "
                        f"{order_side} at {order_price}, current={current_price}"
                    )

            # 2. Logic for STOP_MARKET orders (Stop-losses)
            elif order_type == "STOP_MARKET":
                if (order_side == "BUY" and current_price >= order_price) or (
                    order_side == "SELL" and current_price <= order_price
                ):
                    should_execute = True
                    logger.info(
                        f"[PaperOrderCheck] STOP_MARKET order {order_id} triggered: "
                        f"{order_side} at {order_price}, current={current_price}"
                    )

            if should_execute:
                log_prefix_exec = f"[PaperOrderFill:{symbol}]"
                logger.info(
                    f"{log_prefix_exec} Order execution simulation {order_type} ID={order_id}. "
                    f"Trigger: Market price {current_price} crossed the order price {order_price}."
                )

                # Get entry_client_order_id from the controller for grouping
                entry_cid = None
                exit_type_label = (
                    "STOP_LOSS" if order_type == "STOP_MARKET" else "TAKE_PROFIT"
                )

                if (
                    self.controller
                    and hasattr(self.controller, "_positions_dict_lock")
                    and hasattr(self.controller, "_active_positions")
                ):
                    async with self.controller._positions_dict_lock:
                        if hasattr(self.controller, "_active_position_get"):
                            pos = self.controller._active_position_get(
                                symbol, getattr(self, "market_type", None)
                            )
                        else:
                            pos = self.controller._active_positions.get(symbol)
                        if pos:
                            entry_cid = pos.entry_client_order_id
                            # Determine if this is the final exit
                            remaining_after = pos.remaining_quantity - float(
                                order["origQty"]
                            )
                            if remaining_after <= 1e-9:
                                exit_type_label = f"FINAL_{exit_type_label}"
                            else:
                                exit_type_label = f"PARTIAL_{exit_type_label}"

                # Execute the order as a market order.
                # The place_order method will handle position updates, balance updates, and DB recording itself.
                fill_result = await self.place_order(
                    symbol=symbol,
                    side=order_side,
                    order_type="MARKET",
                    quantity=float(order["origQty"]),
                    strategy_config_id=order.get("strategy_config_id"),
                    entry_client_order_id=entry_cid,
                    exit_type=exit_type_label,
                    signal_details=order.get(
                        "signal_details"
                    ),  # Pass the saved details
                )
                executed_order_ids.append(order_id)

                # Notify the controller about order execution to update remaining_quantity
                if self.controller and not fill_result.get("error"):
                    try:
                        await self._notify_controller_about_fill(
                            symbol=symbol,
                            order_type=order_type,
                            side=order_side,
                            quantity=float(order["origQty"]),
                            price=current_price,
                        )
                    except Exception as e:
                        logger.error(
                            f"{log_prefix_exec} Error notifying controller about fill: {e}",
                            exc_info=True,
                        )

        # Removing executed orders from the list of open ones
        for order_id in executed_order_ids:
            if order_id in self._open_orders:
                del self._open_orders[order_id]
                logger.info(
                    f"[PaperOrderCheck] Removed executed order {order_id} from open orders"
                )

        if executed_order_ids:
            logger.info(
                f"[PaperOrderCheck] Executed {len(executed_order_ids)} orders this check"
            )

    async def fetch_exchange_info(
        self, force_update: bool = False, specific_market_type: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Simulates the response from fetch_exchange_info for paper trading.
        Generates basic but working data for symbols that have a subscription.
        """
        logger.info("[PaperExecutor] Simulating fetch_exchange_info...")

        # Get symbols that are currently required for operation
        active_symbols = await self.data_consumer.get_active_symbols()

        symbols_data = []
        for symbol in active_symbols:
            # Create a fake but plausible structure for each symbol
            symbols_data.append(
                {
                    "symbol": symbol,
                    "pair": symbol,
                    "status": "TRADING",
                    "contractType": "PERPETUAL",  # Safe default value for futures
                    "quoteAsset": "USDT",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                        {
                            "filterType": "LOT_SIZE",
                            "stepSize": "0.001",
                            "minQty": "0.001",
                            "maxQty": "10000",
                        },
                        {"filterType": "MIN_NOTIONAL", "notional": "5.0"},
                    ],
                }
            )

        logger.info(
            f"[PaperExecutor] Generated fake exchange info for {len(symbols_data)} symbols: {active_symbols}"
        )
        return {"symbols": symbols_data}

    async def _notify_controller_about_fill(
        self, symbol: str, order_type: str, side: str, quantity: float, price: float
    ):
        """
        Notifies the controller about a partial position closure when TP/SL is executed.
        This is necessary to synchronize remaining_quantity in LivePosition.
        """
        if not self.controller:
            logger.warning(
                f"[PaperNotifyFill:{symbol}] Controller reference not set, cannot notify about fill."
            )
            return

        log_prefix = f"[PaperNotifyFill:{symbol}]"
        logger.info(
            f"{log_prefix} Notifying controller about {order_type} {side} fill: {quantity} @ {price}"
        )

        # Get the position from the controller
        async with self.controller._positions_dict_lock:
            if hasattr(self.controller, "_active_position_get"):
                position = self.controller._active_position_get(
                    symbol, getattr(self, "market_type", None)
                )
            else:
                position = self.controller._active_positions.get(symbol)
            if not position:
                logger.warning(
                    f"{log_prefix} Position not found in controller. It may have been closed already."
                )
                return

            if position.status != "OPEN":
                logger.debug(
                    f"{log_prefix} Position status is {position.status}, not OPEN. Skipping update."
                )
                return

            # Update remaining_quantity
            old_remaining = position.remaining_quantity
            position.remaining_quantity -= quantity

            logger.info(
                f"{log_prefix} Updated remaining_quantity: {old_remaining:.8f} -> {position.remaining_quantity:.8f}"
            )

            # If the position is fully closed, changing the status
            if position.remaining_quantity <= 1e-9:
                position.status = "CLOSING"
                position.remaining_quantity = 0.0
                logger.info(
                    f"{log_prefix} Position fully closed. Status changed to CLOSING."
                )

                # Cancel all remaining orders
                orders_to_cancel = []
                if position.current_sl_order_id or position.current_sl_client_order_id:
                    orders_to_cancel.append(("SL", position.current_sl_client_order_id))

                for idx, ptp in enumerate(position.partial_tp_orders):
                    if ptp.status not in ["FILLED", "CANCELED"]:
                        orders_to_cancel.append((f"TP{idx}", ptp.client_order_id))

                # Canceling orders
                for order_label, cli_id in orders_to_cancel:
                    if cli_id and cli_id in self._open_orders:
                        del self._open_orders[cli_id]
                        logger.info(
                            f"{log_prefix} Cancelled {order_label} order {cli_id} after position close."
                        )
            elif position.remaining_quantity < 0:
                logger.error(
                    f"{log_prefix} CRITICAL: remaining_quantity became negative! {position.remaining_quantity:.8f}"
                )
                position.remaining_quantity = 0.0

    async def _record_equity_point(self):
        """
        Records the current balance in Redis to build the equity curve.
        Called after each balance change.
        """
        if not self.redis_client:
            logger.debug(
                f"[EquityRecord] Redis client not configured, skipping equity recording for user {self.user_id}"
            )
            return

        try:
            # Get the current balance from the DB
            wallet_assets = await crud.get_paper_wallet(self.db, user_id=self.user_id)
            total_balance = sum(
                asset.balance for asset in wallet_assets if asset.asset == "USDT"
            )

            # Write to Redis Sorted Set
            # Key: equity_history:paper:{user_id}
            # Score: timestamp_ms
            # Value: balance
            timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            redis_key = f"equity_history:paper:{self.user_id}"

            await self.redis_client.zadd(redis_key, {str(total_balance): timestamp_ms})

            # Limiting the number of points (storing the last 30 days)
            thirty_days_ago_ms = timestamp_ms - (30 * 24 * 60 * 60 * 1000)
            await self.redis_client.zremrangebyscore(
                redis_key, "-inf", thirty_days_ago_ms
            )

            logger.debug(
                f"[EquityRecord] Recorded equity point for user {self.user_id}: ${total_balance:.2f} at {timestamp_ms}"
            )

        except Exception as e:
            logger.error(
                f"[EquityRecord] Failed to record equity point for user {self.user_id}: {e}",
                exc_info=True,
            )

    async def initialize_equity_tracking(self):
        """
        Initializes equity tracking by recording the starting balance point.
        Called once at the start of the executor.
        """
        if self._equity_initialized:
            return

        logger.info(
            f"[EquityInit] Initializing equity tracking for user {self.user_id}"
        )
        await self._record_equity_point()
        self._equity_initialized = True
