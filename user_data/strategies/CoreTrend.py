"""CoreTrend — four indicators, time-series momentum, volatility-targeted.

Designed from the research rather than from our previous guesses:

  * **Time-series momentum, not cross-sectional.** Each asset is judged against
    its own history, never ranked against peers. TSM materially outperforms
    cross-sectional momentum in crypto (~32% vs ~15% annualised in the
    literature), largely because crypto's pairwise correlation is so high that
    ranking assets against each other adds little and concentrates risk.

  * **Volatility targeting, not fixed stakes.** Position size scales with the
    inverse of realised volatility so every trade contributes roughly equal
    risk. This is the standard CTA construction and it is specifically what
    dampens momentum crashes — the dominant tail risk in crypto trend systems.

  * **Few trades.** Transaction costs are what kill most published momentum
    results. A 1h timeframe with breakout entries keeps turnover low.

Four indicators, no more. Each has one job and must agree:

  1. Supertrend      — trend direction (our best-performing benchmark)
  2. Donchian high   — the momentum trigger: a genuine N-period breakout
  3. EMA alignment   — multi-horizon confirmation that the trend is real
  4. ATR / vol regime— filters dead tape and blow-off volatility, and sizes

Exits are deliberately asymmetric: a Supertrend flip gets out immediately,
otherwise an ATR trail runs the winner. Our own data showed trailing exits were
the only consistently profitable exit type we had.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import numpy as np
import talib.abstract as ta
from pandas import DataFrame, Series

import sys
from pathlib import Path

# freqtrade loads strategies from user_data/strategies with the repo root not
# on sys.path, so `from core import ...` fails without this.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core import council, skiplog

from freqtrade.persistence import Trade
from freqtrade.strategy import (
    BooleanParameter,
    DecimalParameter,
    IStrategy,
    IntParameter,
    stoploss_from_absolute,
)

log = logging.getLogger(__name__)


def supertrend(df: DataFrame, period: int, multiplier: float) -> Series:
    """Supertrend direction: +1 uptrend, -1 downtrend.

    Standard construction: bands at ATR*multiplier around the HL2 midpoint,
    with the band that matters ratcheting in the direction of the trend so it
    never loosens while the trend holds.
    """
    atr = ta.ATR(df, timeperiod=period)
    hl2 = (df["high"] + df["low"]) / 2.0
    upper = hl2 + multiplier * atr
    lower = hl2 - multiplier * atr

    close = df["close"].to_numpy()
    up = upper.to_numpy()
    lo = lower.to_numpy()
    direction = np.ones(len(df))
    final_up, final_lo = up.copy(), lo.copy()

    for i in range(1, len(df)):
        if np.isnan(up[i]) or np.isnan(lo[i]):
            direction[i] = direction[i - 1]
            continue
        # Bands only tighten toward price while the trend persists.
        final_up[i] = min(up[i], final_up[i - 1]) if close[i - 1] <= final_up[i - 1] else up[i]
        final_lo[i] = max(lo[i], final_lo[i - 1]) if close[i - 1] >= final_lo[i - 1] else lo[i]

        if close[i] > final_up[i]:
            direction[i] = 1
        elif close[i] < final_lo[i]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]

    return Series(direction, index=df.index)


class CoreTrend(IStrategy):
    """Long-only time-series momentum with volatility-targeted sizing."""

    INTERFACE_VERSION = 3
    timeframe = "1h"
    can_short = False
    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    startup_candle_count = 250

    # Backstop only; custom_stoploss does the real work.
    stoploss = -0.25
    use_custom_stoploss = True

    minimal_roi = {"0": 100.0}

    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": True,
        "stoploss_on_exchange_interval": 60,
    }

    # -- 1. Supertrend: direction ------------------------------------------
    p_st_period = IntParameter(7, 21, default=10, space="buy")
    p_st_mult = DecimalParameter(1.5, 4.0, default=3.0, decimals=1, space="buy")

    # -- 2. Donchian: the momentum trigger ---------------------------------
    p_donchian = IntParameter(20, 100, default=48, space="buy")

    # -- 3. EMA alignment: multi-horizon confirmation -----------------------
    p_ema_fast = IntParameter(10, 60, default=24, space="buy")
    p_ema_slow = IntParameter(80, 240, default=168, space="buy")

    # -- 4. Volatility regime ----------------------------------------------
    # Dead tape produces false breakouts; blow-off volatility produces
    # unsurvivable stops. Trade the middle.
    p_vol_min = DecimalParameter(0.002, 0.02, default=0.004, decimals=4, space="buy")
    p_vol_max = DecimalParameter(0.03, 0.15, default=0.08, decimals=3, space="buy")

    # -- exits --------------------------------------------------------------
    p_stop_atr = DecimalParameter(1.5, 5.0, default=3.0, decimals=1, space="sell")
    p_trail_atr = DecimalParameter(1.5, 6.0, default=3.5, decimals=1, space="sell")
    p_trail_after_r = DecimalParameter(0.0, 3.0, default=1.0, decimals=1, space="sell")

    # -- ablation switches --------------------------------------------------
    # Each filter can be switched off so hyperopt decides which ones earn their
    # place, rather than us assuming all four help. A filter that is disabled
    # in the winning configuration is a filter that was costing money.
    p_use_donchian = BooleanParameter(default=True, space="buy")
    p_use_ema = BooleanParameter(default=True, space="buy")
    p_use_vol = BooleanParameter(default=True, space="buy")
    p_use_st_exit = BooleanParameter(default=True, space="sell")

    # -- volatility targeting ----------------------------------------------
    # Annualised volatility each position is sized to contribute. CTAs commonly
    # anchor portfolio vol near 10%; per-position target is higher because
    # positions are neither always-on nor perfectly correlated.
    p_target_vol = DecimalParameter(0.10, 0.60, default=0.30, decimals=2, space="protection")

    @property
    def protections(self) -> list[dict]:
        return [
            # No MaxDrawdown: the validated backtests never ran it, and enabling
            # it cut 2025-01..2026-08 from +64.64% to +20.00%. Drawdown is
            # watched by the owner instead of enforced here.
            {"method": "CooldownPeriod", "stop_duration_candles": 3},
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 72,
                "trade_limit": 4,
                "stop_duration_candles": 24,
                "only_per_pair": False,
            },
        ]

    @property
    def _is_backtest(self) -> bool:
        mode = str(self.config.get("runmode", "")).lower()
        return any(m in mode for m in ("backtest", "hyperopt", "plot", "edge"))

    def _council(self, name: str, fallback):
        """Council override when trading; the committed value in backtests.

        Backtests must never read the council file, or an A/B comparison would
        move underneath itself.
        """
        if self._is_backtest:
            return fallback
        value = council.lever("CoreTrend", name, None)
        return fallback if value is None else value

    def confirm_trade_entry(
        self, pair: str, order_type: str, amount: float, rate: float,
        time_in_force: str, current_time, entry_tag=None, side: str = "long", **kwargs,
    ) -> bool:
        """Last gate before an order. Records refusals that leave no trade row."""
        if self._is_backtest:
            return True
        if not getattr(self, "_entries_allowed", True):
            skiplog.record(strategy="CoreTrend", pair=pair, reason="council_disabled")
            return False
        return True

    def bot_loop_start(self, current_time, **kwargs) -> None:
        """Council stand-down, plus a record of signals lost to a full slot table.

        A saturated slot table is invisible everywhere else: freqtrade simply
        does not call confirm_trade_entry, so nothing is written down.
        """
        self._entries_allowed = self._is_backtest or council.is_enabled("CoreTrend")
        if self._is_backtest:
            return
        try:
            from freqtrade.persistence import Trade as _T
            used = len(_T.get_open_trades())
            cap = int(self.config.get("max_open_trades", 0) or 0)
            if cap and used >= cap:
                skiplog.record(strategy="CoreTrend", pair="*", reason="slots_full",
                               slots_used=used, slots_max=cap)
            else:
                # Slots freed: the next saturation is a new event worth logging.
                skiplog.clear("CoreTrend", "slots_full")
        except Exception:  # noqa: BLE001 - instrumentation must never stop trading
            pass
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Four indicators. Nothing here that does not feed an entry or exit rule."""
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        # Volatility as a fraction of price, so thresholds are comparable
        # across a $78,000 BTC and a $0.30 XRP.
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"]
        dataframe["realised_vol"] = (
            dataframe["close"].pct_change().rolling(72).std() * np.sqrt(24 * 365)
        )

        dataframe["st_dir"] = supertrend(
            dataframe, int(self.p_st_period.value), float(self.p_st_mult.value)
        )

        # Donchian high excludes the current candle: the breakout must clear
        # prior highs, not the one it is busy setting.
        dataframe["donchian_high"] = (
            dataframe["high"].rolling(int(self.p_donchian.value)).max().shift(1)
        )

        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=int(self.p_ema_fast.value))
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=int(self.p_ema_slow.value))
        dataframe["ema_slow_rising"] = dataframe["ema_slow"] > dataframe["ema_slow"].shift(12)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """All four must agree. Any one disagreeing means no trade."""
        # 1. Supertrend direction is the one non-negotiable filter: it was the
        #    single best-performing signal in our benchmark on its own.
        conditions = (dataframe["st_dir"] > 0) & (dataframe["volume"] > 0)

        if self.p_use_donchian.value:                                    # 2 breakout
            conditions &= dataframe["close"] > dataframe["donchian_high"]
        if self.p_use_ema.value:                                         # 3 alignment
            conditions &= (dataframe["ema_fast"] > dataframe["ema_slow"]) & dataframe["ema_slow_rising"]
        if self.p_use_vol.value:                                         # 4 vol regime
            conditions &= (dataframe["atr_pct"] > float(self.p_vol_min.value)) & (
                dataframe["atr_pct"] < float(self.p_vol_max.value)
            )

        dataframe.loc[conditions, ["enter_long", "enter_tag"]] = (1, "coretrend")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Supertrend flip is the thesis breaking; everything else trails out."""
        if self.p_use_st_exit.value:
            dataframe.loc[dataframe["st_dir"] < 0, ["exit_long", "exit_tag"]] = (1, "st_flip")
        return dataframe

    def custom_stake_amount(
        self, pair: str, current_time: datetime, current_rate: float,
        proposed_stake: float, min_stake: Optional[float], max_stake: float,
        leverage: float, entry_tag: Optional[str], side: str, **kwargs,
    ) -> float:
        """Volatility targeting: size inversely to the pair's realised vol.

        A pair running at 120% annualised vol gets a quarter the size of one at
        30%, so each position contributes comparable risk. This is what stops a
        single violent asset from dominating the book, and it is the mechanism
        the literature credits with damping momentum crashes.
        """
        frame, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if frame is None or frame.empty:
            return proposed_stake

        vol = float(frame["realised_vol"].iloc[-1] or 0.0)
        if not np.isfinite(vol) or vol <= 0:
            return proposed_stake

        target = float(self.p_target_vol.value)
        scaled = proposed_stake * (target / vol)
        # Never scale *up* beyond the proposed allocation: volatility targeting
        # is here to reduce risk in wild markets, not to add leverage in calm
        # ones, which is how vol-targeting blows up.
        scaled = min(scaled, proposed_stake)
        if not self._is_backtest:
            # Global risk dial. Only ever shrinks a position, never grows it.
            scaled *= council.size_multiplier()

        if min_stake is not None and scaled < min_stake:
            return min_stake if min_stake <= max_stake else 0.0
        return max(0.0, min(scaled, max_stake))

    def custom_stoploss(
        self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
        current_profit: float, after_fill: bool, **kwargs,
    ) -> Optional[float]:
        """Fixed ATR stop until the trade is up N×risk, then an ATR trail."""
        frame, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if frame is None or frame.empty:
            return None

        atr = float(frame["atr"].iloc[-1] or 0.0)
        if atr <= 0 or trade.open_rate <= 0:
            return None

        risk = float(self._council("stop_atr", self.p_stop_atr.value)) * atr
        initial_stop = trade.open_rate - risk
        activation = trade.open_rate + float(
            self._council("trail_after_r", self.p_trail_after_r.value)) * risk
        peak = max(float(trade.max_rate or 0.0), current_rate)

        if peak < activation:
            stop_price = initial_stop
        else:
            trail = float(self._council("trail_atr", self.p_trail_atr.value)) * atr
            stop_price = max(initial_stop, peak - trail)

        return stoploss_from_absolute(
            stop_price, current_rate, is_short=trade.is_short, leverage=trade.leverage
        )
