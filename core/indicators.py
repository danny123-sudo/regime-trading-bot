"""Technical indicator stack and normalised scoring.

Every indicator returns a score in [-1.0, +1.0] where positive is bullish.
Scores are combined into a composite using the regime's weight profile.

Indicator math is implemented directly on pandas rather than via pandas-ta:
the library's current release breaks on numpy >= 2, and these are standard
formulas that we would rather own, test, and trust than pin a dependency for.
All calculations run on closed candles only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def _wilder(series: pd.Series, length: int) -> pd.Series:
    """Wilder's smoothing, expressed as an EWM with alpha = 1/length."""
    return series.ewm(alpha=1.0 / length, adjust=False).mean()


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(length).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    ranges = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    return _wilder(true_range(df), length)


def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = _wilder(delta.clip(lower=0.0), length)
    loss = _wilder((-delta).clip(lower=0.0), length)
    # Expressed as gain/(gain+loss) rather than via RS so that a zero-loss
    # window yields 100 rather than dividing by zero, and a flat series
    # (both zero) falls through to the neutral 50.
    total = gain + loss
    return (100.0 * gain / total.replace(0.0, np.nan)).fillna(50.0)


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(series, fast) - ema(series, slow)
    sig = ema(line, signal)
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def stochastic(df: pd.DataFrame, k: int = 14, d: int = 3, smooth: int = 3) -> pd.DataFrame:
    low_k = df["low"].rolling(k).min()
    high_k = df["high"].rolling(k).max()
    raw = 100.0 * (df["close"] - low_k) / (high_k - low_k).replace(0.0, np.nan)
    k_line = raw.rolling(smooth).mean()
    return pd.DataFrame({"k": k_line, "d": k_line.rolling(d).mean()})


def williams_r(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high = df["high"].rolling(length).max()
    low = df["low"].rolling(length).min()
    return -100.0 * (high - df["close"]) / (high - low).replace(0.0, np.nan)


def adx(df: pd.DataFrame, length: int = 14) -> pd.DataFrame:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = _wilder(true_range(df), length).replace(0.0, np.nan)
    plus_di = 100.0 * _wilder(plus_dm, length) / tr
    minus_di = 100.0 * _wilder(minus_dm, length) / tr
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return pd.DataFrame({"adx": _wilder(dx.fillna(0.0), length), "plus_di": plus_di, "minus_di": minus_di})


def aroon(df: pd.DataFrame, length: int = 25) -> pd.DataFrame:
    def _since_high(x: np.ndarray) -> float:
        return float(len(x) - 1 - int(np.argmax(x)))

    def _since_low(x: np.ndarray) -> float:
        return float(len(x) - 1 - int(np.argmin(x)))

    periods_since_high = df["high"].rolling(length + 1).apply(_since_high, raw=True)
    periods_since_low = df["low"].rolling(length + 1).apply(_since_low, raw=True)
    up = 100.0 * (length - periods_since_high) / length
    down = 100.0 * (length - periods_since_low) / length
    return pd.DataFrame({"up": up, "down": down, "osc": up - down})


def bollinger(series: pd.Series, length: int = 20, std: float = 2.0) -> pd.DataFrame:
    mid = sma(series, length)
    dev = series.rolling(length).std(ddof=0)
    return pd.DataFrame({"mid": mid, "upper": mid + std * dev, "lower": mid - std * dev})


def keltner(df: pd.DataFrame, length: int = 20, mult: float = 1.5) -> pd.DataFrame:
    mid = ema(df["close"], length)
    rng = atr(df, length) * mult
    return pd.DataFrame({"mid": mid, "upper": mid + rng, "lower": mid - rng})


def obv(df: pd.DataFrame) -> pd.Series:
    direction = np.sign(df["close"].diff().fillna(0.0))
    return (direction * df["volume"]).cumsum()


def ad_line(df: pd.DataFrame) -> pd.Series:
    span = (df["high"] - df["low"]).replace(0.0, np.nan)
    mfm = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / span
    return (mfm.fillna(0.0) * df["volume"]).cumsum()


def vwap(df: pd.DataFrame, length: int = 96) -> pd.Series:
    """Rolling VWAP. 96 x 15m candles approximates a 24h session."""
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = (typical * df["volume"]).rolling(length).sum()
    vol = df["volume"].rolling(length).sum().replace(0.0, np.nan)
    return pv / vol


def psar(df: pd.DataFrame, step: float = 0.02, max_step: float = 0.2) -> pd.Series:
    """Parabolic SAR. Iterative by nature; only used when ADX > 25."""
    high, low = df["high"].to_numpy(), df["low"].to_numpy()
    n = len(df)
    out = np.full(n, np.nan)
    if n < 2:
        return pd.Series(out, index=df.index)

    bullish = True
    af = step
    ep = high[0]
    sar = low[0]
    for i in range(1, n):
        prev_sar = sar
        sar = prev_sar + af * (ep - prev_sar)
        if bullish:
            sar = min(sar, low[i - 1], low[max(i - 2, 0)])
            if low[i] < sar:
                bullish, sar, ep, af = False, ep, low[i], step
            elif high[i] > ep:
                ep, af = high[i], min(af + step, max_step)
        else:
            sar = max(sar, high[i - 1], high[max(i - 2, 0)])
            if high[i] > sar:
                bullish, sar, ep, af = True, ep, high[i], step
            elif low[i] < ep:
                ep, af = low[i], min(af + step, max_step)
        out[i] = sar
    return pd.Series(out, index=df.index)


def ichimoku(df: pd.DataFrame) -> pd.DataFrame:
    """Ichimoku cloud. Spans are shifted forward as the standard requires."""
    conv = (df["high"].rolling(9).max() + df["low"].rolling(9).min()) / 2.0
    base = (df["high"].rolling(26).max() + df["low"].rolling(26).min()) / 2.0
    span_a = ((conv + base) / 2.0).shift(26)
    span_b = ((df["high"].rolling(52).max() + df["low"].rolling(52).min()) / 2.0).shift(26)
    return pd.DataFrame({"conv": conv, "base": base, "span_a": span_a, "span_b": span_b})


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------


def _clip(value: float) -> float:
    """Clamp to the [-1, 1] score range, mapping NaN to neutral."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return 0.0
    return float(max(-1.0, min(1.0, value)))


def _scale(value: float, lo: float, hi: float) -> float:
    """Linearly map ``value`` from [lo, hi] onto [-1, 1]."""
    if hi == lo or math.isnan(value):
        return 0.0
    return _clip(2.0 * (value - lo) / (hi - lo) - 1.0)


@dataclass
class IndicatorSet:
    """Computed indicators for one pair on one timeframe, plus their scores."""

    close: float
    atr: float
    adx: float
    plus_di: float
    minus_di: float
    rsi: float
    bb_position: float           # 0 = lower band, 1 = upper band
    bb_upper: float
    bb_lower: float
    cloud_position: int          # +1 above, 0 inside, -1 below
    squeeze_on: bool
    volume_ratio: float          # current volume / 20-period average
    obv_divergence: int          # +1 bullish, -1 bearish, 0 none
    scores: dict[str, float] = field(default_factory=dict)
    # The same oscillators read as continuation rather than reversal. Which set
    # applies is a property of the regime, not of the indicator — see
    # composite_score(momentum_orientation=...).
    momentum_scores: dict[str, float] = field(default_factory=dict)
    raw: dict[str, float] = field(default_factory=dict)


def _obv_divergence(close: pd.Series, obv_series: pd.Series, lookback: int = 20) -> int:
    """Detect price/OBV divergence over ``lookback`` candles.

    Price making a new high while OBV does not is distribution (-1);
    price making a new low while OBV does not is accumulation (+1).
    The highest-quality signal in the stack, so it is flagged explicitly.
    """
    if len(close) < lookback + 1:
        return 0
    recent_close, prior_close = close.iloc[-1], close.iloc[-lookback:-1]
    recent_obv, prior_obv = obv_series.iloc[-1], obv_series.iloc[-lookback:-1]
    if recent_close >= prior_close.max() and recent_obv < prior_obv.max():
        return -1
    if recent_close <= prior_close.min() and recent_obv > prior_obv.min():
        return 1
    return 0


def compute(df: pd.DataFrame) -> IndicatorSet:
    """Compute the full indicator stack on a closed-candle DataFrame.

    Args:
        df: OHLCV frame with columns open/high/low/close/volume, oldest first.
            Requires at least ~120 rows for the slower indicators to be valid.

    Returns:
        An IndicatorSet with raw values and per-indicator scores in [-1, 1].
    """
    if len(df) < 60:
        raise ValueError(f"need at least 60 candles to compute indicators, got {len(df)}")

    close = df["close"]
    last = close.iloc[-1]

    ema9, ema21 = ema(close, 9), ema(close, 21)
    ema50, ema200 = ema(close, 50), ema(close, 200) if len(df) >= 200 else (ema(close, 50), None)
    macd_df = macd(close)
    aroon_df = aroon(df)
    psar_series = psar(df)
    rsi_series = rsi(close)
    stoch_df = stochastic(df)
    adx_df = adx(df)
    will = williams_r(df)
    obv_series = obv(df)
    ad_series = ad_line(df)
    vwap_series = vwap(df)
    bb = bollinger(close)
    kc = keltner(df)
    atr_series = atr(df)
    ich = ichimoku(df)

    atr_val = float(atr_series.iloc[-1])
    adx_val = float(adx_df["adx"].iloc[-1])

    # --- trend scores -----------------------------------------------------
    ema_fast_spread = (ema9.iloc[-1] - ema21.iloc[-1]) / max(atr_val, 1e-9)
    ema_score = _clip(ema_fast_spread)
    if ema200 is not None and not math.isnan(ema200.iloc[-1]):
        regime_bias = 1.0 if ema50.iloc[-1] > ema200.iloc[-1] else -1.0
        ema_score = _clip(0.7 * ema_score + 0.3 * regime_bias)

    hist = macd_df["hist"]
    macd_score = _clip(float(hist.iloc[-1]) / max(float(hist.abs().rolling(50).mean().iloc[-1]), 1e-9))

    aroon_score = _clip(float(aroon_df["osc"].iloc[-1]) / 100.0)

    psar_val = float(psar_series.iloc[-1]) if not math.isnan(psar_series.iloc[-1]) else last
    psar_score = _clip((1.0 if last > psar_val else -1.0) if adx_val > 25 else 0.0)

    cloud_top = float(np.nanmax([ich["span_a"].iloc[-1], ich["span_b"].iloc[-1]]))
    cloud_bottom = float(np.nanmin([ich["span_a"].iloc[-1], ich["span_b"].iloc[-1]]))
    if math.isnan(cloud_top) or math.isnan(cloud_bottom):
        cloud_position = 0
    elif last > cloud_top:
        cloud_position = 1
    elif last < cloud_bottom:
        cloud_position = -1
    else:
        cloud_position = 0
    ichimoku_score = float(cloud_position)

    # --- momentum scores --------------------------------------------------
    # Oscillators are scored twice. In a range, an oversold reading is a buy
    # (reversion). In a trend, the same reading is weakness (continuation).
    # Blending the two orientations in one composite makes trend and momentum
    # signals cancel, which is why both are kept and the regime picks.
    rsi_val = float(rsi_series.iloc[-1])
    rsi_score = _clip(-_scale(rsi_val, 30.0, 70.0))
    rsi_momentum = _clip(_scale(rsi_val, 40.0, 60.0))
    stoch_k = float(stoch_df["k"].iloc[-1]) if not math.isnan(stoch_df["k"].iloc[-1]) else 50.0
    stoch_score = _clip(-_scale(stoch_k, 20.0, 80.0))
    stoch_momentum = _clip(_scale(stoch_k, 40.0, 60.0))
    will_val = float(will.iloc[-1]) if not math.isnan(will.iloc[-1]) else -50.0
    will_score = _clip(-_scale(will_val, -80.0, -20.0))
    will_momentum = _clip(_scale(will_val, -60.0, -40.0))
    di_spread = float(adx_df["plus_di"].iloc[-1] - adx_df["minus_di"].iloc[-1])
    adx_score = _clip(di_spread / 25.0) * _clip(adx_val / 40.0)

    # --- volume scores ----------------------------------------------------
    obv_slope = obv_series.diff(10).iloc[-1]
    obv_score = _clip(float(obv_slope) / max(float(obv_series.diff(10).abs().rolling(50).mean().iloc[-1]), 1e-9))
    ad_slope = ad_series.diff(10).iloc[-1]
    ad_score = _clip(float(ad_slope) / max(float(ad_series.diff(10).abs().rolling(50).mean().iloc[-1]), 1e-9))
    vwap_val = float(vwap_series.iloc[-1]) if not math.isnan(vwap_series.iloc[-1]) else last
    vwap_score = _clip(-(last - vwap_val) / max(atr_val, 1e-9))  # below VWAP = value

    # --- volatility scores ------------------------------------------------
    bb_upper, bb_lower = float(bb["upper"].iloc[-1]), float(bb["lower"].iloc[-1])
    bb_span = max(bb_upper - bb_lower, 1e-9)
    bb_position = (last - bb_lower) / bb_span
    bb_score = _clip(-_scale(bb_position, 0.0, 1.0))  # near lower band = bullish

    squeeze_on = bool(bb_upper < float(kc["upper"].iloc[-1]) and bb_lower > float(kc["lower"].iloc[-1]))
    squeeze_score = 0.0  # direction comes from volume; the squeeze itself is neutral

    vol_avg = float(df["volume"].rolling(20).mean().iloc[-1])
    volume_ratio = float(df["volume"].iloc[-1]) / max(vol_avg, 1e-9)

    divergence = _obv_divergence(close, obv_series)

    scores = {
        "ema": ema_score, "macd": macd_score, "aroon": aroon_score, "psar": psar_score,
        "ichimoku": ichimoku_score, "rsi": rsi_score, "stoch": stoch_score,
        "williams": will_score, "adx": adx_score, "obv": obv_score, "ad": ad_score,
        "vwap": vwap_score, "bb": bb_score, "squeeze": squeeze_score,
    }
    momentum_scores = {
        "rsi": rsi_momentum, "stoch": stoch_momentum, "williams": will_momentum,
        # In a trend, stretched price is continuation, not a fade.
        "bb": _clip(_scale(float(bb_position), 0.4, 0.6)),
        "vwap": _clip((last - vwap_val) / max(atr_val, 1e-9)),
    }
    raw = {
        "close": float(last), "atr": atr_val, "adx": adx_val, "rsi": rsi_val,
        "stoch_k": stoch_k, "williams_r": will_val, "bb_position": float(bb_position),
        "volume_ratio": volume_ratio, "vwap": vwap_val, "ema9": float(ema9.iloc[-1]),
        "ema21": float(ema21.iloc[-1]), "macd_hist": float(hist.iloc[-1]),
        "cloud_top": cloud_top, "cloud_bottom": cloud_bottom,
    }

    return IndicatorSet(
        close=float(last), atr=atr_val, adx=adx_val,
        plus_di=float(adx_df["plus_di"].iloc[-1]), minus_di=float(adx_df["minus_di"].iloc[-1]),
        rsi=rsi_val, bb_position=float(bb_position), bb_upper=bb_upper, bb_lower=bb_lower,
        cloud_position=cloud_position, squeeze_on=squeeze_on, volume_ratio=volume_ratio,
        obv_divergence=divergence, scores=scores, momentum_scores=momentum_scores, raw=raw,
    )


def composite_score(
    ind: IndicatorSet,
    weights: dict[str, float],
    sentiment: float = 0.0,
    sentiment_weight: float = 0.0,
    momentum_orientation: bool = False,
) -> tuple[float, list[tuple[str, float]]]:
    """Weighted composite of indicator scores, plus the top 3 contributors.

    Args:
        ind: The computed indicator set.
        weights: Per-indicator weights for the active regime.
        sentiment: X-sentiment score in [-1, 1].
        sentiment_weight: Bounded weight for sentiment; never dominant.
        momentum_orientation: True in trending/breakout regimes, where
            oscillators are read as continuation rather than reversal. Getting
            this wrong makes momentum and trend signals cancel and the
            composite never leaves the middle of its range.

    Returns:
        (composite in [-1, 1], [(name, contribution), ...] top 3 by magnitude)
    """
    contributions: list[tuple[str, float]] = []
    total_weight = 0.0
    total = 0.0
    for name, score in ind.scores.items():
        weight = float(weights.get(name, 0.0))
        if weight <= 0.0:
            continue
        if momentum_orientation and name in ind.momentum_scores:
            score = ind.momentum_scores[name]
        contribution = weight * score
        total += contribution
        total_weight += weight
        contributions.append((name, contribution))

    if sentiment_weight > 0.0:
        contribution = sentiment_weight * _clip(sentiment)
        total += contribution
        total_weight += sentiment_weight
        contributions.append(("sentiment", contribution))

    composite = total / total_weight if total_weight > 0 else 0.0
    composite += 0.10 * ind.obv_divergence
    contributions.sort(key=lambda item: abs(item[1]), reverse=True)
    return _clip(composite), contributions[:3]
