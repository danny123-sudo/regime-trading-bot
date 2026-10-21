"""Regime-aware composite strategy.

Our IP, riding freqtrade's chassis. Freqtrade owns data, execution, stops,
Telegram, protections, backtesting and hyperopt; this class owns the decision
logic:

  * a regime classifier (TRENDING / RANGING / BREAKOUT_WATCH / AMBIGUOUS)
  * a regime-weighted composite of 14 indicator scores, where oscillators flip
    orientation between reversion and continuation depending on the regime
  * regime-specific entry preconditions (volume, band position, cloud bias)
  * an ATR trail that only engages once a trade is up N units of initial risk
  * a daily risk budget, expressed in risk rather than trade count
  * a Grok confirmation veto, applied live only (never during backtests)

Two sources of truth for tunables, and they do not fight:

  * **Hyperopt parameters** below are the baseline, searched offline and
    written to the strategy's parameter file. They apply in every mode.
  * **Council levers** in ``user_data/strategy_config.json`` override the
    baseline in live/dry-run only, inside bounds the council cannot widen.

A backtest therefore always reflects the committed baseline, so an A/B or a
hyperopt run is never moving underneath itself.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from pandas import DataFrame

# The repo root holds the `core` package; freqtrade loads this file from
# user_data/strategies, so make the root importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from freqtrade.persistence import Trade  # noqa: E402
from freqtrade.strategy import (  # noqa: E402
    DecimalParameter,
    IStrategy,
    IntParameter,
    stoploss_from_absolute,
)

from core import features  # noqa: E402
from core.levers import DEFAULT_LEVERS  # noqa: E402
from core.strategy_state import StrategyState  # noqa: E402

log = logging.getLogger(__name__)


class RegimeComposite(IStrategy):
    """Long-only regime/composite strategy for KuCoin spot."""

    INTERFACE_VERSION = 3

    timeframe = "15m"
    can_short = False
    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    startup_candle_count = 210

    # A wide backstop. Real protection is custom_stoploss plus the
    # exchange-side stop, not this ceiling.
    stoploss = -0.20
    use_custom_stoploss = True

    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": True,
        "stoploss_on_exchange_interval": 60,
    }
    order_time_in_force = {"entry": "GTC", "exit": "GTC"}
    minimal_roi = {"0": 100.0}

    # ------------------------------------------------------------------
    # Hyperopt parameters
    # ------------------------------------------------------------------
    # Entry side: how much conviction, how much volume, how selective.
    p_threshold = DecimalParameter(0.30, 0.80, default=0.65, decimals=2, space="buy")
    p_volume_mult = DecimalParameter(1.0, 2.0, default=1.3, decimals=2, space="buy")
    p_adx_trend_min = IntParameter(15, 30, default=20, space="buy")
    p_range_entry_bb = DecimalParameter(0.20, 0.60, default=0.50, decimals=2, space="buy")

    # Exit side: stop width and how long a winner runs before the trail bites.
    # R:R is the cheaper of the two paths to break-even, so this is where the
    # search has the most room.
    p_stop_mult = DecimalParameter(1.0, 3.5, default=2.5, decimals=1, space="sell")
    p_trail_after_r = DecimalParameter(0.0, 4.0, default=3.0, decimals=1, space="sell")
    p_range_exit_bb = DecimalParameter(0.60, 0.95, default=0.75, decimals=2, space="sell")

    # Risk budget: total equity at risk across open positions per day. This
    # replaces a flat "max entries per day" count — five trades with 3% stops
    # is triple the exposure of five with 1% stops, so budget in risk, not tally.
    p_daily_risk_budget = DecimalParameter(0.01, 0.06, default=0.02, decimals=3, space="protection")

    @property
    def protections(self) -> list[dict]:
        """Freqtrade-native circuit breakers, independent of strategy logic."""
        return [
            {"method": "CooldownPeriod", "stop_duration_candles": 2},
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 96,
                "trade_limit": 5,
                "stop_duration_candles": 96,
                "max_allowed_drawdown": 0.10,
            },
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 96,
                "trade_limit": 3,
                "stop_duration_candles": 48,
                "only_per_pair": False,
            },
            {
                "method": "LowProfitPairs",
                "lookback_period_candles": 360,
                "trade_limit": 4,
                "stop_duration_candles": 60,
                "required_profit": -0.05,
            },
        ]

    plot_config = {
        "main_plot": {},
        "subplots": {
            "composite": {"composite": {"color": "blue"}},
            "regime": {"regime": {"color": "orange"}},
            "adx": {"adx": {"color": "red"}},
        },
    }

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.state = StrategyState(runmode=str(config.get("runmode", "")))

    # -- tunables ----------------------------------------------------------
    def lever(self, name: str, parameter_value):
        """Resolve a tunable: council override in live, parameter otherwise.

        The council may only move a value inside its bound, and only when the
        bot is actually trading. Backtests and hyperopt always see the
        committed baseline.
        """
        if self.state.is_backtest:
            return parameter_value
        return self.state.lever(name, parameter_value)

    # -- indicators --------------------------------------------------------
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Compute the full indicator stack, regime, and composite."""
        dataframe = features.populate(dataframe)

        weights = self.state.regime_weights()
        if weights != DEFAULT_LEVERS["regime_weights"]:
            dataframe["composite"] = features.recompute_composite(dataframe, weights)

        # Sentiment has no historical series, so using it in a backtest would
        # be lookahead. It is a live-only nudge and reads 0.0 otherwise.
        sentiment = self.state.sentiment_for(metadata["pair"])
        weight = float(self.lever("sentiment_weight", DEFAULT_LEVERS["sentiment_weight"]))
        dataframe["sentiment"] = sentiment
        if weight > 0 and sentiment != 0.0:
            dataframe["composite"] = (
                dataframe["composite"] * (1 - weight) + sentiment * weight
            ).clip(-1.0, 1.0)
        return dataframe

    # -- entries -----------------------------------------------------------
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Long entries: conviction, cloud bias, regime rules, volume."""
        threshold = float(self.lever("composite_threshold", self.p_threshold.value))
        volume_mult = float(self.lever("volume_filter_mult", self.p_volume_mult.value))
        adx_min = int(self.p_adx_trend_min.value)
        range_bb = float(self.p_range_entry_bb.value)
        enabled = self.state.regime_enabled_map()

        conviction = dataframe["composite"] >= threshold
        above_cloud = dataframe["cloud_position"] == 1
        has_volume = dataframe["volume"] > 0

        trending = (
            (dataframe["regime"] == features.TRENDING)
            & enabled["TRENDING"]
            & (dataframe["adx"] >= adx_min)
            & (dataframe["plus_di"] > dataframe["minus_di"])
        )

        # Mean reversion: buy the lower part of the band, with volume
        # confirmation — the filter separating a fade from a falling knife.
        ranging = (
            (dataframe["regime"] == features.RANGING)
            & enabled["RANGING"]
            & (dataframe["adx"] <= 30)
            & (dataframe["bb_position"] <= range_bb)
            & (dataframe["volume_ratio"] >= volume_mult)
        )

        breakout = (
            (dataframe["regime"] == features.BREAKOUT_WATCH)
            & enabled["BREAKOUT_WATCH"]
            & (dataframe["s_obv"] > 0)
            & (dataframe["s_ad"] > 0)
            & (dataframe["volume_ratio"] >= volume_mult)
        )

        dataframe.loc[
            conviction & above_cloud & has_volume & (trending | ranging | breakout),
            ["enter_long", "enter_tag"],
        ] = (1, "composite")
        return dataframe

    # -- exits -------------------------------------------------------------
    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Signal exits: conviction reversal, range target, or lost bias."""
        threshold = float(self.lever("composite_threshold", self.p_threshold.value))
        exit_bb = float(self.p_range_exit_bb.value)

        reversal = dataframe["composite"] <= -threshold
        range_target = (dataframe["regime"] == features.RANGING) & (
            dataframe["bb_position"] >= exit_bb
        )
        lost_bias = (dataframe["regime"] == features.TRENDING) & (
            dataframe["cloud_position"] == -1
        )

        dataframe.loc[reversal | range_target | lost_bias, ["exit_long", "exit_tag"]] = (1, "signal")
        return dataframe

    def custom_stoploss(
        self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
        current_profit: float, after_fill: bool, **kwargs,
    ) -> Optional[float]:
        """ATR stop that only starts trailing once the trade is up N×risk.

        Trailing from the entry candle turns trend trades into 45-minute
        scalps that lose to noise — measured, not assumed.
        """
        frame, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if frame is None or frame.empty:
            return None

        atr = float(frame["atr"].iloc[-1] or 0.0)
        if atr <= 0 or trade.open_rate <= 0:
            return None

        stop_mult = float(self.lever("atr_stop_mult", self.p_stop_mult.value))
        trail_after_r = float(self.lever("trail_after_r", self.p_trail_after_r.value))

        risk = stop_mult * atr
        initial_stop = trade.open_rate - risk
        activation = trade.open_rate + trail_after_r * risk
        peak = max(float(trade.max_rate or 0.0), current_rate)

        stop_price = initial_stop if peak < activation else max(initial_stop, peak - risk)
        return stoploss_from_absolute(
            stop_price, current_rate, is_short=trade.is_short, leverage=trade.leverage
        )

    # -- risk budget --------------------------------------------------------
    def confirm_trade_entry(
        self, pair: str, order_type: str, amount: float, rate: float,
        time_in_force: str, current_time: datetime, entry_tag: Optional[str],
        side: str, **kwargs,
    ) -> bool:
        """Daily risk budget, then Grok's veto.

        The budget replaces a flat daily trade count: what matters is how much
        equity is exposed, not how many tickets were written. A wide stop
        consumes more budget and therefore permits fewer trades, automatically.
        """
        if not self._within_risk_budget(pair, rate, current_time):
            return False

        # Grok is a live-only confirmation step; backtests measure the
        # deterministic layer alone, which keeps lever comparisons honest.
        if self.state.is_backtest:
            return True

        frame, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if frame is None or frame.empty:
            return True
        return self.state.confirm_entry(pair, frame.iloc[-1], rate, self.lever)

    def _within_risk_budget(self, pair: str, rate: float, current_time: datetime) -> bool:
        """True if this trade's risk fits inside today's remaining budget."""
        budget = float(self.lever("daily_risk_budget", self.p_daily_risk_budget.value))
        if budget <= 0:
            return True

        frame, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if frame is None or frame.empty:
            return True
        atr = float(frame["atr"].iloc[-1] or 0.0)
        if atr <= 0 or rate <= 0:
            return True

        stop_mult = float(self.lever("atr_stop_mult", self.p_stop_mult.value))
        stake = float(self.wallets.get_trade_stake_amount(pair, self.config["max_open_trades"]))
        equity = float(self.wallets.get_total_stake_amount())
        if equity <= 0:
            return True

        # Risk of this trade as a fraction of equity: stake * stop distance %.
        trade_risk = (stake * (stop_mult * atr / rate)) / equity
        used = self._risk_used_today(current_time, equity)

        if used + trade_risk > budget:
            log.info(
                "%s entry skipped: daily risk budget %.2f%% would be exceeded "
                "(used %.2f%%, this trade %.2f%%)",
                pair, budget * 100, used * 100, trade_risk * 100,
            )
            return False
        return True

    def _risk_used_today(self, current_time: datetime, equity: float) -> float:
        """Risk already committed today: realised losses plus open exposure."""
        if equity <= 0:
            return 0.0
        start = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)

        used = 0.0
        try:
            trades = Trade.get_trades_proxy(open_date=start)
        except Exception:  # noqa: BLE001 - never block trading on bookkeeping
            return 0.0

        stop_mult = float(self.lever("atr_stop_mult", self.p_stop_mult.value))
        for trade in trades:
            if trade.is_open:
                frame, _ = self.dp.get_analyzed_dataframe(trade.pair, self.timeframe)
                atr = float(frame["atr"].iloc[-1]) if frame is not None and not frame.empty else 0.0
                if atr > 0 and trade.open_rate > 0:
                    used += (trade.stake_amount * (stop_mult * atr / trade.open_rate)) / equity
            else:
                realised = trade.close_profit_abs or 0.0
                if realised < 0:
                    used += abs(realised) / equity
        return used
