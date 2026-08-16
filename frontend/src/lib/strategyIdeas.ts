/**
 * Curated Strategy Ideas Library
 * ───────────────────────────────
 * 27 hand-picked, plain-English strategy recipes for the AI Co-pilot.
 * Each entry has:
 *   - title, summary, risk profile
 *   - timeframe and best market
 *   - copy-paste prompt for the AI Co-pilot
 *   - tags (style) for filtering
 *
 * Source of truth: `DepthSight - Strategy Ideas Library.md` (Obsidian vault).
 * Keep this file in sync with that document.
 */

export type StrategyStyle =
  | "Day Trading"
  | "Swing"
  | "Position"
  | "Scalping"
  | "Mean Reversion"
  | "Trend"
  | "Defensive";

export type RiskLevel = "Low" | "Medium" | "High";

export interface StrategyIdea {
  id: string;
  title: string;
  style: StrategyStyle;
  risk: RiskLevel;
  timeframe: string;
  bestFor: string;
  summary: string;
  /** Plain-English prompt to paste into the AI Co-pilot. */
  prompt: string;
  /** Optional ready-made config snippet (paste into the JSON editor). */
  configSnippet?: string;
  tags: string[];
}

export const STRATEGY_IDEAS: StrategyIdea[] = [
  // ── Day trading ──────────────────────────────────────────────────────
  {
    id: "rsi-breakout-v2",
    title: "RSI Breakout v2",
    style: "Day Trading",
    risk: "Medium",
    timeframe: "1h-4h",
    bestFor: "Trending BTC/ETH",
    summary:
      "Buy when RSI crosses above 50 from below with a 20-EMA uptrend. Sell when RSI > 70 or hits 2% take-profit.",
    prompt:
      "Build a day-trading strategy for BTC-USDT on the 1h chart. Entry: long when RSI(14) crosses above 50 from below AND price is above EMA(20). Exit: RSI > 70 OR price hits +2% take-profit from entry. Stop-loss: -1% from entry. Position size: 5% of account. Use 5x leverage. Run on Binance Futures.",
    configSnippet: JSON.stringify(
      {
        symbol: "BTCUSDT",
        timeframe: "1h",
        entry: { rsi_period: 14, rsi_threshold: 50, ema_period: 20 },
        exit: { take_profit_pct: 2, stop_loss_pct: 1, rsi_exit: 70 },
        position: { size_pct: 5, leverage: 5 },
      },
      null,
      2,
    ),
    tags: ["rsi", "ema", "breakout", "popular"],
  },
  {
    id: "ema-crossover-9-21",
    title: "EMA Crossover 9/21",
    style: "Day Trading",
    risk: "Medium",
    timeframe: "15m-1h",
    bestFor: "Active crypto pairs",
    summary:
      "Classic fast/slow EMA crossover. Go long on golden cross, exit on death cross. Trend-following with built-in stop.",
    prompt:
      "Create a strategy using EMA 9 and EMA 21 crossover on the 15m chart for ETH-USDT. Long when EMA9 crosses above EMA21. Close long when EMA9 crosses below EMA21. Add 1.5% stop-loss and 3% take-profit. Position size 3% per trade.",
    tags: ["ema", "crossover", "classic"],
  },
  {
    id: "macd-divergence",
    title: "MACD Divergence",
    style: "Day Trading",
    risk: "Medium",
    timeframe: "1h-4h",
    bestFor: "Reversal entries",
    summary:
      "Buy when price makes lower low but MACD histogram makes higher low (bullish divergence). Strong reversal signal.",
    prompt:
      "Build a strategy for BTC-USDT 4h. Detect bullish MACD divergence: price makes a lower low over last 50 candles but MACD histogram makes a higher low. Enter long on next candle. Stop below the lower low. Take profit at 1.618 Fibonacci extension. Position size 2%.",
    tags: ["macd", "divergence", "reversal"],
  },
  {
    id: "vwap-bounce",
    title: "VWAP Bounce",
    style: "Day Trading",
    risk: "Low",
    timeframe: "5m-15m",
    bestFor: "Intraday mean reversion",
    summary:
      "Buy when price pulls back to VWAP and shows rejection (wick). Tight stop, ride the bounce to upper band.",
    prompt:
      "Strategy: VWAP bounce on BTC-USDT 5m. Buy when price touches VWAP from above and the candle has a lower wick ≥ 30% of candle range. Stop 0.3% below entry. Take profit 0.6% above entry. Trail stop once 0.4% in profit.",
    tags: ["vwap", "mean reversion", "scalp"],
  },

  // ── Swing ────────────────────────────────────────────────────────────
  {
    id: "swing-bollinger-squeeze",
    title: "Bollinger Squeeze Breakout",
    style: "Swing",
    risk: "Medium",
    timeframe: "4h-Daily",
    bestFor: "Volatility expansion",
    summary:
      "Wait for Bollinger Bands to squeeze (low width). Enter on the breakout candle in either direction. Use ADX to confirm trend strength.",
    prompt:
      "Build a swing strategy using Bollinger Bands (20, 2) on the 4h chart. Detect squeeze: BB width below 4% for 20+ candles. On the breakout candle (close outside the band), enter in that direction. Confirm with ADX > 25. Stop at opposite band. Take profit at 2x ATR(14) from entry.",
    tags: ["bollinger", "squeeze", "volatility"],
  },
  {
    id: "rsi-oversold-bounce",
    title: "RSI Oversold Bounce (Swing)",
    style: "Swing",
    risk: "Low",
    timeframe: "Daily",
    bestFor: "Pullback entries",
    summary:
      "Buy established uptrends when RSI dips below 35 and reclaims 40. Solid risk/reward for swing traders.",
    prompt:
      "Swing strategy: BTC-USDT daily. Only trade when price is above EMA(200) (uptrend). Buy when RSI(14) drops below 35 then closes back above 40. Stop 5% below entry. Take profit 12% above entry. Use 7% position size. No leverage.",
    tags: ["rsi", "pullback", "uptrend"],
  },
  {
    id: "weekly-pivot-reversal",
    title: "Weekly Pivot Reversal",
    style: "Swing",
    risk: "Low",
    timeframe: "Daily-Weekly",
    bestFor: "Major support/resistance",
    summary:
      "Trade bounces off classic pivot points (R1, S1) with confirmation from a bullish engulfing candle.",
    prompt:
      "Strategy on ETH-USDT daily. Calculate classic pivots from prior week's high/low/close. Buy when price touches S1 or S2 and the daily candle closes as a hammer or bullish engulfing. Stop 2% below the low of the signal candle. Take profit at pivot (P) or R1, whichever comes first. Position 4%.",
    tags: ["pivot", "support", "reversal"],
  },

  // ── Position ─────────────────────────────────────────────────────────
  {
    id: "btc-dca-pro",
    title: "BTC DCA Pro (Position)",
    style: "Position",
    risk: "Low",
    timeframe: "Weekly",
    bestFor: "Long-term accumulation",
    summary:
      "Dollar-cost average into BTC weekly. Boost buys by 50% when price is below 200-day MA.",
    prompt:
      "Build a position/DCA strategy for BTC-USDT weekly timeframe. Every Monday, buy $100 of BTC. If close is below 200-week MA, increase the buy to $150. If close is above 200-week MA * 1.5, skip the buy. Take profit: sell 20% of holdings when price hits 3x the average cost. No leverage.",
    tags: ["dca", "long-term", "passive"],
  },
  {
    id: "eth-staking-momentum",
    title: "ETH Momentum + Staking Yield",
    style: "Position",
    risk: "Medium",
    timeframe: "Daily-Weekly",
    bestFor: "Multi-week holds",
    summary:
      "Hold ETH when 50-day MA > 200-day MA. Use 20% of the position for active swing trades on the remainder.",
    prompt:
      "Position strategy on ETH-USDT daily. Enter full position when EMA(50) > EMA(200). Allocate 80% as core hold, 20% as swing reserve. When RSI(14) > 70, sell 5% of total position. When RSI(14) < 30, use swing reserve to buy. Rebalance monthly. No leverage.",
    tags: ["eth", "momentum", "yield"],
  },
  {
    id: "macro-trend-filter",
    title: "Macro Trend Filter",
    style: "Position",
    risk: "Low",
    timeframe: "Daily",
    bestFor: "Stay in the right trend",
    summary:
      "Only take long positions when BTC is above its 200-day MA and the macro trend (weekly) is up.",
    prompt:
      "Long-only position strategy. Universe: top-10 by market cap on Binance. Trade the daily chart. Entry filter: BTC 200-DMA trending up (close > MA) AND coin's 50-DMA > 200-DMA. Buy on pullback: price retraces 38.2% of last 30-day range with RSI < 40. Stop 8% below entry. Take profit at -1 retracement. Position 3% per trade.",
    tags: ["macro", "trend", "rotation"],
  },

  // ── Scalping ─────────────────────────────────────────────────────────
  {
    id: "5m-stochastic-scalp",
    title: "5-Min Stochastic Scalp",
    style: "Scalping",
    risk: "High",
    timeframe: "1m-5m",
    bestFor: "Active sessions",
    summary:
      "Quick mean-reversion on 5m. Buy oversold, sell overbought. Strict 0.3% target / 0.2% stop.",
    prompt:
      "Scalp strategy: SOL-USDT 5m. Long when Stochastic(14,3,3) %K < 20 AND %K crosses above %D. Short when %K > 80 AND %K crosses below %D. Take profit 0.3%. Stop loss 0.2%. Maximum 3 open positions. Maximum 30 trades per day. No overnight holds.",
    tags: ["stochastic", "scalp", "quick"],
  },
  {
    id: "orderbook-imbalance",
    title: "Order-Book Imbalance Scalp",
    style: "Scalping",
    risk: "High",
    timeframe: "1m",
    bestFor: "Liquid pairs (BTC/ETH)",
    summary:
      "Detect 70%+ bid/ask imbalance in top 20 levels. Fade the imbalance on 1m chart. Sub-minute holds.",
    prompt:
      "Build a 1m scalp strategy for BTCUSDT. Each minute, read order book top 20 levels. If bid volume / (bid+ask) > 0.70, go long. If ask volume / (bid+ask) > 0.70, go short. Hold for max 5 candles. Take profit 0.15%. Stop loss 0.10%. Cancel if no fill in 60s.",
    tags: ["orderbook", "microstructure", "hft-lite"],
  },
  {
    id: "atr-breakout-scalp",
    title: "ATR Breakout Scalp",
    style: "Scalping",
    risk: "High",
    timeframe: "5m",
    bestFor: "Opening range breakouts",
    summary:
      "Wait for first 15 minutes of session. Enter breakout of high/low with 0.5x ATR stop.",
    prompt:
      "Scalp strategy for BTCUSDT 5m. Define session open = 00:00 UTC. Track first 15 minutes' high and low. After 00:15, if price breaks above the high, go long; if below the low, go short. Stop 0.5x ATR(14). Take profit 1.0x ATR(14). Flatten all positions at 23:45 UTC. Position 1% of account.",
    tags: ["breakout", "session", "atr"],
  },

  // ── Mean Reversion ──────────────────────────────────────────────────
  {
    id: "bollinger-mean-reversion",
    title: "Bollinger Mean Reversion",
    style: "Mean Reversion",
    risk: "Medium",
    timeframe: "1h-4h",
    bestFor: "Range-bound markets",
    summary:
      "Fade extremes. Buy when price tags lower band, short when upper band. Target = middle band (SMA20).",
    prompt:
      "Mean reversion strategy for BTCUSDT 1h. Use Bollinger Bands (20, 2). Long when close < lower band AND RSI(14) < 30. Short when close > upper band AND RSI(14) > 70. Exit at the middle band (SMA20). Stop at 1.5x ATR(14). Max 2 open positions. Skip signals when ADX(14) > 25 (trending).",
    tags: ["bollinger", "mean reversion", "range"],
  },
  {
    id: "z-score-reversion",
    title: "Z-Score Reversion",
    style: "Mean Reversion",
    risk: "Medium",
    timeframe: "4h",
    bestFor: "Quant-style entries",
    summary:
      "Compute z-score of close vs 50-period mean. Enter at z < -2, exit at z = 0.",
    prompt:
      "Build a strategy for ETHUSDT 4h. Compute z-score = (close - SMA(50)) / StdDev(50) over the last 50 candles. Long when z < -2. Exit when z returns to 0. Short when z > 2. Stop at z = -3 (or +3 for short). Position 3% per trade. No leverage.",
    tags: ["z-score", "statistical", "quant"],
  },
  {
    id: "funding-rate-fade",
    title: "Funding Rate Fade",
    style: "Mean Reversion",
    risk: "High",
    timeframe: "4h-8h",
    bestFor: "Crowded perp trades",
    summary:
      "When funding > 0.05%, market is over-leveraged long. Short with tight stop. Fade the herd.",
    prompt:
      "Strategy on perpetual futures (BTCUSDT-PERP) 4h. Read funding rate every 8h. When funding > 0.05% AND funding has been positive for 3+ periods, open short. Stop 1.5% above entry. Take profit when funding turns negative OR price hits 3% profit. Position 2% of account, 3x leverage.",
    tags: ["funding", "perp", "crowded"],
  },

  // ── Trend ────────────────────────────────────────────────────────────
  {
    id: "donchian-turtle",
    title: "Donchian Turtle Trend",
    style: "Trend",
    risk: "Medium",
    timeframe: "Daily",
    bestFor: "Classic trend following",
    summary:
      "20-day Donchian breakout. The original Turtle Trading system, modernized for crypto.",
    prompt:
      "Trend-following strategy for BTCUSDT daily. Long when close > highest high of last 20 days. Short when close < lowest low of last 20 days. Stop at 2x ATR(20) from entry. Take profit at 6x ATR(20) from entry. Position 4% per trade, 2x leverage. Pyramiding: add 50% at +2x ATR.",
    tags: ["donchian", "turtle", "trend"],
  },
  {
    id: "supertrend-classic",
    title: "SuperTrend Classic",
    style: "Trend",
    risk: "Medium",
    timeframe: "4h-Daily",
    bestFor: "Strong trends",
    summary:
      "ATR-based trend indicator. Long when price above SuperTrend, short when below. Ride big moves.",
    prompt:
      "Strategy using SuperTrend (10-period, 3x ATR multiplier) on BTCUSDT 4h. Long when close crosses above SuperTrend line. Short when close crosses below. Trail stop using SuperTrend line. Position 5% per trade, 3x leverage. Take profit when ADX(14) < 20 (trend weakening).",
    tags: ["supertrend", "atr", "trend"],
  },
  {
    id: "ichimoku-cloud",
    title: "Ichimoku Cloud Breakout",
    style: "Trend",
    risk: "Medium",
    timeframe: "4h-Daily",
    bestFor: "Multi-timeframe trend",
    summary:
      "Trade breakouts above/below the Kumo (cloud). Confluence with Tenkan/Kijun cross adds confidence.",
    prompt:
      "Ichimoku strategy on ETHUSDT 4h. Long when: (1) close > top of Kumo cloud, (2) Tenkan (9) > Kijun (26), (3) Chikou (26) > close 26 periods ago. Short on opposite. Stop below Kumo (or above for shorts). Take profit at 1.5x ATR(14). Position 4% per trade.",
    tags: ["ichimoku", "cloud", "mtf"],
  },
  {
    id: "200ma-trend-hold",
    title: "200-DMA Trend Hold",
    style: "Trend",
    risk: "Low",
    timeframe: "Weekly",
    bestFor: "Long-term trend capture",
    summary:
      "Buy when weekly close > 200-DMA, exit when close < 200-DMA. Simple, robust, low time-in-market.",
    prompt:
      "Long-only trend strategy for BTCUSDT weekly. Buy next bar's open when weekly close > SMA(200) AND it was below the SMA last week (fresh cross). Sell next bar's open when weekly close < SMA(200) (exit cross). Position 10% of account per entry. Pyramid: add another 5% for each 20% gain.",
    tags: ["sma", "long-term", "patient"],
  },

  // ── Defensive ────────────────────────────────────────────────────────
  {
    id: "stable-yield-rotation",
    title: "Stablecoin Yield Rotation",
    style: "Defensive",
    risk: "Low",
    timeframe: "Daily",
    bestFor: "Cash-equivalent yield",
    summary:
      "Rotate between USDT and USDC based on best available on-chain lending yield. Capital preservation.",
    prompt:
      "Defensive strategy for stablecoins. Universe: USDT, USDC, DAI. Daily, check lending rates on Aave/Compound. Move 100% of stable balance to the highest-yielding asset if the spread is > 0.5% APY. Otherwise hold. Target: maximize yield, minimize swap frequency (no swaps more than 1x per week).",
    tags: ["stable", "yield", "passive"],
  },
  {
    id: "vix-spike-buy",
    title: "Crypto Fear & Greed Spike Buy",
    style: "Defensive",
    risk: "Medium",
    timeframe: "Daily",
    bestFor: "Buying panic dips",
    summary:
      "When fear index drops below 25, scale into BTC over 5 days. Historically strong entries.",
    prompt:
      "Strategy: read the Crypto Fear & Greed Index daily. When it drops below 25 (extreme fear), buy 2% of account into BTC. If it stays below 25 for 3+ consecutive days, add another 2%. Maximum 10% of account. Hold for 90 days minimum. No stop loss.",
    tags: ["sentiment", "contrarian", "btc"],
  },
  {
    id: "volatility-targeting",
    title: "Volatility Targeting",
    style: "Defensive",
    risk: "Low",
    timeframe: "Daily",
    bestFor: "Risk-managed exposure",
    summary:
      "Scale position size inversely with realized volatility. Target constant 20% annualized vol.",
    prompt:
      "Volatility-targeting strategy for BTCUSDT daily. Target annualized volatility = 20%. Compute 20-day realized vol. Position size = (0.20 / realized_vol) * base_allocation. Cap position between 1% and 15% of account. Rebalance daily. No leverage.",
    tags: ["vol-targeting", "risk-parity", "institutional"],
  },
  {
    id: "correlation-breaker",
    title: "Correlation Breaker Hedge",
    style: "Defensive",
    risk: "Low",
    timeframe: "Daily",
    bestFor: "Hedging portfolio risk",
    summary:
      "When BTC-SPX correlation > 0.6, hedge 30% of crypto exposure with short perpetual or put options.",
    prompt:
      "Portfolio hedge strategy. Daily, compute 30-day rolling correlation between BTCUSDT and SPX. When correlation > 0.6, open a hedge: short 30% of crypto long exposure using BTC-PERP (no leverage). When correlation drops below 0.3, close the hedge. Track cost: 0.05% per day funding.",
    tags: ["hedge", "correlation", "macro"],
  },
  {
    id: "drawdown-protector",
    title: "Drawdown Protector",
    style: "Defensive",
    risk: "Low",
    timeframe: "Daily",
    bestFor: "Capital preservation",
    summary:
      "Reduce exposure by 50% when portfolio drawdown exceeds 10%. Re-enter gradually when recovered.",
    prompt:
      "Risk-management overlay (not a standalone strategy). Track running portfolio equity. If drawdown from peak > 10%, cut all open positions to 50% size. If drawdown > 20%, cut to 25%. Re-enter gradually: when in profit, restore positions over 5 days. Applies to all other strategies you run.",
    tags: ["risk", "drawdown", "overlay"],
  },
  {
    id: "weekend-flat",
    title: "Weekend Flat Strategy",
    style: "Defensive",
    risk: "Low",
    timeframe: "Daily",
    bestFor: "Avoiding weekend gaps",
    summary:
      "Close all positions before Friday 22:00 UTC. Re-enter Monday 02:00 UTC. Avoids thin liquidity.",
    prompt:
      "Defensive overlay strategy. Universe: any other strategy. Every Friday at 22:00 UTC, close all open positions. Hold cash over the weekend. Every Monday at 02:00 UTC, allow new entries. Track reduction in max drawdown vs. always-on baseline. Position sizes unchanged during the week.",
    tags: ["weekend", "liquidity", "overlay"],
  },
  {
    id: "btc-dip-buyer-50dma",
    title: "BTC Dip Buyer (50-DMA)",
    style: "Defensive",
    risk: "Low",
    timeframe: "Daily",
    bestFor: "Recurring buys",
    summary:
      "Buy BTC when price dips 10%+ below 50-DMA. Sell half at +20% from buy, ride the rest.",
    prompt:
      "Simple long-only strategy for BTCUSDT daily. Calculate 50-DMA. When close < DMA * 0.90 (10%+ below), buy 3% of account. Each buy gets its own stop at -8% and take profit at +20%. When 20% target is hit, sell half; let the rest ride with a trailing stop at the 50-DMA.",
    tags: ["btc", "dip", "patient"],
  },
];

/** All unique styles for the filter chips. */
export const ALL_STYLES: StrategyStyle[] = [
  "Day Trading",
  "Swing",
  "Position",
  "Scalping",
  "Mean Reversion",
  "Trend",
  "Defensive",
];

/** All unique risk levels. */
export const ALL_RISKS: RiskLevel[] = ["Low", "Medium", "High"];
