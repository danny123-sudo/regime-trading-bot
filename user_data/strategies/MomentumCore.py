"""MomentumCore — built from the signal study rather than from intuition.

The research (research/signal_study.py, research/decile_test.py) found one
robust, tradeable effect across 25 pairs and 3.3 years:

    horizon        best signal          net edge after 0.16% fees
    4h forward     none                 negative — pure noise
    24h forward    mom_48               +0.395%
    7d forward     mom_12 (daily)       +1.185%

Two conclusions drive this strategy. Multi-day momentum is real and its edge
grows with holding period. And there is no exploitable edge at intraday
horizons once fees are paid, which is why every previous strategy in this repo
lost money — they all traded 15m-1h candles with hours-long holds.

So: slow candles, long holds, few trades.

  * Entry needs three things to agree — the asset's own multi-day momentum,
    BTC's market-level momentum (btc_mom carried a positive IC at every
    timeframe tested), and a volatility regime that is neither dead nor
    blowing off.
  * Exit is a wide ATR trail that only engages once the trade is well in
    profit. Cutting a momentum winner early is how you convert a positive
    expectancy into a negative one — measured twice on this project.
  * Turnover is deliberately low. At a few trades per pair per month the
    0.16% round trip stops being the dominant term.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import numpy as np
import talib.abstract as ta
from pandas import DataFrame

from freqtrade.persistence import Trade
import sys
from pathlib import Path

# freqtrade loads strategies from user_data/strategies with the repo root not
# on sys.path, so `from core import ...` fails without this.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core import council, skiplog

from freqtrade.strategy import (
    BooleanParameter,
    DecimalParameter,
    IStrategy,
    IntParameter,
    stoploss_from_absolute,
)

log = logging.getLogger(__name__)


class MomentumCore(IStrategy):
    """Multi-day time-series momentum, long only."""

    INTERFACE_VERSION = 3
    timeframe = "4h"          # override with --timeframe 1d
    can_short = False
    process_only_new_candles = True
    use_exit_signal = True
    startup_candle_count = 220

    stoploss = -0.30          # backstop; custom_stoploss governs
    use_custom_stoploss = True
    minimal_roi = {"0": 100.0}

    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": True,
        "stoploss_on_exchange_interval": 300,
    }

    # --- entry ------------------------------------------------------------
    # Lookback is in candles, so 12 means 2 days at 4h and 12 days at 1d. The
    # study found the edge in both regions, so the same range serves both.
    p_mom_lookback = IntParameter(6, 60, default=12, space="buy")
    p_mom_min = DecimalParameter(0.0, 0.15, default=0.02, decimals=3, space="buy")

    # BTC as the market factor: alts inherit market direction, and btc_mom
    # carried a positive IC at 1h, 4h and 1d alike.
    p_use_btc_filter = BooleanParameter(default=True, space="buy")
    p_btc_lookback = IntParameter(6, 60, default=24, space="buy")

    # Trend confirmation.
    p_use_ema = BooleanParameter(default=True, space="buy")
    p_ema_slow = IntParameter(20, 120, default=50, space="buy")

    # Volatility regime.
    p_use_vol = BooleanParameter(default=True, space="buy")
    p_vol_max = DecimalParameter(0.02, 0.25, default=0.12, decimals=3, space="buy")

    # --- exit -------------------------------------------------------------
    p_stop_atr = DecimalParameter(2.0, 8.0, default=4.0, decimals=1, space="sell")
    p_trail_atr = DecimalParameter(2.0, 10.0, default=5.0, decimals=1, space="sell")
    p_trail_after_r = DecimalParameter(0.0, 4.0, default=1.5, decimals=1, space="sell")
    p_exit_on_mom_loss = BooleanParameter(default=True, space="sell")

    @property
    def protections(self) -> list[dict]:
        # No MaxDrawdown: the validated backtests never ran it, and it locked
        # the live bot out of the market from 2026-09-14 on summed trade ratios
        # (26%) when the wallet was down about 7%. Drawdown is watched by the
        # owner instead of enforced here.
        return [
            {"method": "CooldownPeriod", "stop_duration_candles": 2},
        ]

    def _council(self, name: str, fallback):
        """Council override when trading; the committed value in backtests."""
        if self._is_backtest:
            return fallback
        return council.lever("MomentumCore", name, None)

    @property
    def _is_backtest(self) -> bool:
        mode = str(self.config.get("runmode", "")).lower()
        return any(m in mode for m in ("backtest", "hyperopt", "plot", "edge"))

    def confirm_trade_entry(
        self, pair: str, order_type: str, amount: float, rate: float,
        time_in_force: str, current_time, entry_tag=None, side: str = "long", **kwargs,
    ) -> bool:
        """Last gate before an order. Records refusals that leave no trade row."""
        if self._is_backtest:
            return True
        if not getattr(self, "_entries_allowed", True):
            skiplog.record(strategy="MomentumCore", pair=pair, reason="council_disabled")
            return False
        return True

    def bot_loop_start(self, current_time, **kwargs) -> None:
        """Council stand-down, plus a record of signals lost to a full slot table.

        A saturated slot table is invisible everywhere else: freqtrade simply
        does not call confirm_trade_entry, so nothing is written down.
        """
        self._entries_allowed = self._is_backtest or council.is_enabled("MomentumCore")
        if self._is_backtest:
            return
        try:
            from freqtrade.persistence import Trade as _T
            used = len(_T.get_open_trades())
            cap = int(self.config.get("max_open_trades", 0) or 0)
            if cap and used >= cap:
                skiplog.record(strategy="MomentumCore", pair="*", reason="slots_full",
                               slots_used=used, slots_max=cap)
            else:
                # Slots freed: the next saturation is a new event worth logging.
                skiplog.clear("MomentumCore", "slots_full")
        except Exception:  # noqa: BLE001 - instrumentation must never stop trading
            pass
    def informative_pairs(self) -> list[tuple[str, str]]:
        return [("BTC/USDT", self.timeframe)]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        close = dataframe["close"]

        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / close
        dataframe["mom"] = close.pct_change(int(self.p_mom_lookback.value))
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=int(self.p_ema_slow.value))

        # Market factor. If BTC data is unavailable the filter is neutralised
        # rather than blocking every trade.
        try:
            btc = self.dp.get_pair_dataframe("BTC/USDT", self.timeframe)
            if btc is not None and not btc.empty:
                btc_mom = btc["close"].pct_change(int(self.p_btc_lookback.value))
                merged = btc[["date"]].copy()
                merged["btc_mom"] = btc_mom.to_numpy()
                dataframe = dataframe.merge(merged, on="date", how="left")
                dataframe["btc_mom"] = dataframe["btc_mom"].ffill().fillna(0.0)
            else:
                dataframe["btc_mom"] = 0.0
        except Exception:  # noqa: BLE001 - a missing informative must not stop trading
            dataframe["btc_mom"] = 0.0

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = (
            (dataframe["mom"] > float(self._council("mom_min", self.p_mom_min.value)))
            & (dataframe["volume"] > 0)
        )
        if self.p_use_btc_filter.value:
            conditions &= dataframe["btc_mom"] > 0
        if self.p_use_ema.value:
            conditions &= dataframe["close"] > dataframe["ema_slow"]
        if self.p_use_vol.value:
            conditions &= dataframe["atr_pct"] < float(self.p_vol_max.value)

        if not getattr(self, "_entries_allowed", True):
            return dataframe   # council stood this strategy down

        dataframe.loc[conditions, ["enter_long", "enter_tag"]] = (1, "momentum")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Exit when the reason for the trade stops being true."""
        if self.p_exit_on_mom_loss.value:
            dataframe.loc[dataframe["mom"] < 0, ["exit_long", "exit_tag"]] = (1, "mom_lost")
        return dataframe

    def custom_stoploss(
        self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
        current_profit: float, after_fill: bool, **kwargs,
    ) -> Optional[float]:
        """Fixed ATR stop until the trade is up N x risk, then a wide trail."""
        frame, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if frame is None or frame.empty:
            return None

        atr = float(frame["atr"].iloc[-1] or 0.0)
        if atr <= 0 or trade.open_rate <= 0 or not np.isfinite(atr):
            return None

        risk = float(self._council("stop_atr", self.p_stop_atr.value)) * atr
        initial_stop = trade.open_rate - risk
        activation = trade.open_rate + float(
            self._council("trail_after_r", self.p_trail_after_r.value)) * risk
        peak = max(float(trade.max_rate or 0.0), current_rate)

        trail = float(self._council("trail_atr", self.p_trail_atr.value)) * atr
        stop_price = initial_stop if peak < activation else max(initial_stop, peak - trail)
        return stoploss_from_absolute(
            stop_price, current_rate, is_short=trade.is_short, leverage=trade.leverage
        )
