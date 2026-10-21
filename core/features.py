"""Vectorised feature engineering: indicator scores, regime, composite.

This is the same logic as the per-candle path in ``indicators.py`` and
``regime.py``, expressed over whole columns so freqtrade's backtester can run
it once per pair instead of once per candle. The scalar version remains the
reference implementation and the two are checked against each other in tests.

Everything here is pure pandas in, pandas out. No exchange, no network, no LLM.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from core import indicators as ta
from core.levers import DEFAULT_LEVERS

# Regime codes. Integers rather than strings so the column stays numeric and
# vectorised selection is cheap.
AMBIGUOUS, TRENDING, RANGING, BREAKOUT_WATCH = 0, 1, 2, 3
REGIME_NAMES = {AMBIGUOUS: "AMBIGUOUS", TRENDING: "TRENDING",
                RANGING: "RANGING", BREAKOUT_WATCH: "BREAKOUT_WATCH"}
NAME_TO_CODE = {v: k for k, v in REGIME_NAMES.items()}

# Regimes that read oscillators as continuation rather than reversal.
MOMENTUM_REGIMES = (TRENDING, BREAKOUT_WATCH)

SCORE_COLUMNS = ("ema", "macd", "aroon", "psar", "ichimoku", "rsi", "stoch",
                 "williams", "adx", "obv", "ad", "vwap", "bb", "squeeze")


def _clip(series: pd.Series) -> pd.Series:
    return series.clip(-1.0, 1.0).fillna(0.0)


def _scale(series: pd.Series, lo: float, hi: float) -> pd.Series:
    """Map [lo, hi] onto [-1, 1], clipped."""
    if hi == lo:
        return pd.Series(0.0, index=series.index)
    return _clip(2.0 * (series - lo) / (hi - lo) - 1.0)


def populate(df: pd.DataFrame) -> pd.DataFrame:
    """Add every indicator, score, regime, and composite column to ``df``.

    Args:
        df: OHLCV frame, oldest first, with open/high/low/close/volume.

    Returns:
        The same frame with feature columns appended. Rows before the
        indicators converge carry NaN/neutral values and must not be traded;
        the strategy enforces a startup candle count for that.
    """
    out = df.copy()
    close = out["close"]

    # -- raw indicators ----------------------------------------------------
    ema9, ema21 = ta.ema(close, 9), ta.ema(close, 21)
    ema50, ema200 = ta.ema(close, 50), ta.ema(close, 200)
    macd_df = ta.macd(close)
    aroon_df = ta.aroon(out)
    psar = ta.psar(out)
    rsi = ta.rsi(close)
    stoch = ta.stochastic(out)
    adx_df = ta.adx(out)
    will = ta.williams_r(out)
    obv = ta.obv(out)
    ad = ta.ad_line(out)
    vwap = ta.vwap(out)
    bb = ta.bollinger(close)
    kc = ta.keltner(out)
    atr = ta.atr(out)
    ich = ta.ichimoku(out)

    out["atr"] = atr
    out["adx"] = adx_df["adx"]
    out["plus_di"] = adx_df["plus_di"]
    out["minus_di"] = adx_df["minus_di"]
    out["rsi"] = rsi

    safe_atr = atr.replace(0.0, np.nan)

    # -- cloud position ----------------------------------------------------
    cloud_top = pd.concat([ich["span_a"], ich["span_b"]], axis=1).max(axis=1)
    cloud_bottom = pd.concat([ich["span_a"], ich["span_b"]], axis=1).min(axis=1)
    out["cloud_position"] = np.select(
        [close > cloud_top, close < cloud_bottom], [1, -1], default=0
    )

    # -- Bollinger / squeeze ------------------------------------------------
    bb_span = (bb["upper"] - bb["lower"]).replace(0.0, np.nan)
    out["bb_position"] = ((close - bb["lower"]) / bb_span).fillna(0.5)
    out["squeeze_on"] = (bb["upper"] < kc["upper"]) & (bb["lower"] > kc["lower"])

    vol_avg = out["volume"].rolling(20).mean().replace(0.0, np.nan)
    out["volume_ratio"] = (out["volume"] / vol_avg).fillna(1.0)

    # -- trend scores -------------------------------------------------------
    ema_spread = _clip((ema9 - ema21) / safe_atr)
    regime_bias = pd.Series(np.where(ema50 > ema200, 1.0, -1.0), index=out.index)
    out["s_ema"] = _clip(0.7 * ema_spread + 0.3 * regime_bias)

    hist = macd_df["hist"]
    hist_scale = hist.abs().rolling(50).mean().replace(0.0, np.nan)
    out["s_macd"] = _clip(hist / hist_scale)

    out["s_aroon"] = _clip(aroon_df["osc"] / 100.0)

    # Parabolic SAR only carries weight in a confirmed trend.
    psar_dir = pd.Series(np.where(close > psar, 1.0, -1.0), index=out.index)
    out["s_psar"] = _clip(psar_dir.where(out["adx"] > 25, 0.0))

    out["s_ichimoku"] = out["cloud_position"].astype(float)

    # -- momentum scores, both orientations ---------------------------------
    out["s_rsi"] = -_scale(rsi, 30.0, 70.0)
    out["m_rsi"] = _scale(rsi, 40.0, 60.0)
    stoch_k = stoch["k"].fillna(50.0)
    out["s_stoch"] = -_scale(stoch_k, 20.0, 80.0)
    out["m_stoch"] = _scale(stoch_k, 40.0, 60.0)
    will_f = will.fillna(-50.0)
    out["s_williams"] = -_scale(will_f, -80.0, -20.0)
    out["m_williams"] = _scale(will_f, -60.0, -40.0)

    di_spread = adx_df["plus_di"] - adx_df["minus_di"]
    out["s_adx"] = _clip(di_spread / 25.0) * _clip(out["adx"] / 40.0)

    # -- volume scores ------------------------------------------------------
    obv_slope = obv.diff(10)
    obv_scale = obv_slope.abs().rolling(50).mean().replace(0.0, np.nan)
    out["s_obv"] = _clip(obv_slope / obv_scale)
    ad_slope = ad.diff(10)
    ad_scale = ad_slope.abs().rolling(50).mean().replace(0.0, np.nan)
    out["s_ad"] = _clip(ad_slope / ad_scale)

    vwap_f = vwap.fillna(close)
    out["s_vwap"] = _clip(-(close - vwap_f) / safe_atr)   # below VWAP = value
    out["m_vwap"] = _clip((close - vwap_f) / safe_atr)    # above VWAP = strength

    # -- volatility scores --------------------------------------------------
    out["s_bb"] = -_scale(out["bb_position"], 0.0, 1.0)   # lower band = buy
    out["m_bb"] = _scale(out["bb_position"], 0.4, 0.6)    # stretched = continuation
    out["s_squeeze"] = 0.0

    # -- OBV divergence -----------------------------------------------------
    out["obv_divergence"] = _divergence(close, obv)

    # -- regime -------------------------------------------------------------
    out["regime"] = _classify(out)

    # -- composite ----------------------------------------------------------
    out["composite"] = _composite(out, DEFAULT_LEVERS["regime_weights"])
    return out


def _divergence(close: pd.Series, obv: pd.Series, lookback: int = 20) -> pd.Series:
    """Vectorised price/OBV divergence flag: +1 accumulation, -1 distribution."""
    prior_close_max = close.shift(1).rolling(lookback - 1).max()
    prior_close_min = close.shift(1).rolling(lookback - 1).min()
    prior_obv_max = obv.shift(1).rolling(lookback - 1).max()
    prior_obv_min = obv.shift(1).rolling(lookback - 1).min()
    bearish = (close >= prior_close_max) & (obv < prior_obv_max)
    bullish = (close <= prior_close_min) & (obv > prior_obv_min)
    return pd.Series(np.select([bullish, bearish], [1, -1], default=0), index=close.index)


def _classify(df: pd.DataFrame) -> pd.Series:
    """Vectorised regime classification, matching regime.classify()."""
    adx = df["adx"]
    prev_adx = adx.shift(1)
    adx_rising = adx > prev_adx
    direction = np.where(df["plus_di"] > df["minus_di"], 1, -1)

    # Order mirrors the scalar classifier: squeeze wins over range.
    breakout = df["squeeze_on"] & (adx < 25) & (adx_rising | (adx >= 20))

    outside_bands = (df["bb_position"] > 0.9) | (df["bb_position"] < 0.1)
    cloud_agrees = df["cloud_position"] == direction
    trending = (adx > 25) & (outside_bands | cloud_agrees)

    ranging = (adx < 20) & (df["bb_position"] > 0.05) & (df["bb_position"] < 0.95)

    return pd.Series(
        np.select([breakout, trending, ranging],
                  [BREAKOUT_WATCH, TRENDING, RANGING], default=AMBIGUOUS),
        index=df.index,
    )


def _composite(df: pd.DataFrame, weights_by_regime: dict) -> pd.Series:
    """Weighted composite, using each regime's weights and orientation.

    Computed once per regime profile then selected row-wise, which keeps the
    whole thing vectorised while still honouring per-regime weights.
    """
    per_regime: dict[int, pd.Series] = {}
    for name, weights in weights_by_regime.items():
        code = NAME_TO_CODE.get(name)
        if code is None:
            continue
        momentum = code in MOMENTUM_REGIMES
        total = pd.Series(0.0, index=df.index)
        total_weight = 0.0
        for indicator in SCORE_COLUMNS:
            weight = float(weights.get(indicator, 0.0))
            if weight <= 0.0:
                continue
            column = f"m_{indicator}" if momentum and f"m_{indicator}" in df else f"s_{indicator}"
            total = total + weight * df[column]
            total_weight += weight
        per_regime[code] = total / total_weight if total_weight else total

    composite = pd.Series(0.0, index=df.index)
    for code, series in per_regime.items():
        composite = composite.where(df["regime"] != code, series)

    # Divergence is the highest-quality signal, so it earns a bounded bonus.
    composite = composite + 0.10 * df["obv_divergence"]
    return composite.clip(-1.0, 1.0).fillna(0.0)


def recompute_composite(df: pd.DataFrame, weights_by_regime: dict) -> pd.Series:
    """Recompute the composite under a different weight set.

    Used by the adaptive layer's backtest gate to score a candidate lever set
    without re-deriving every indicator.
    """
    return _composite(df, weights_by_regime)
