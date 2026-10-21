"""Indicator correctness and scoring bounds.

Reference values are computed from first principles rather than another
library, so these tests pin the maths we actually ship.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core import indicators


def test_ema_matches_manual_recursion() -> None:
    series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    result = indicators.ema(series, 3)
    alpha = 2 / (3 + 1)
    expected = [1.0]
    for value in series[1:]:
        expected.append(alpha * value + (1 - alpha) * expected[-1])
    assert result.to_numpy() == pytest.approx(np.array(expected))


def test_rsi_is_100_on_a_pure_uptrend() -> None:
    series = pd.Series(np.arange(1, 60, dtype=float))
    assert indicators.rsi(series).iloc[-1] == pytest.approx(100.0, abs=1e-6)


def test_rsi_is_zero_on_a_pure_downtrend() -> None:
    series = pd.Series(np.arange(60, 1, -1, dtype=float))
    assert indicators.rsi(series).iloc[-1] == pytest.approx(0.0, abs=1e-6)


def test_rsi_of_a_flat_series_is_neutral() -> None:
    series = pd.Series([100.0] * 50)
    assert indicators.rsi(series).iloc[-1] == pytest.approx(50.0)


def test_true_range_uses_the_widest_of_the_three_measures() -> None:
    frame = pd.DataFrame({
        "high": [10.0, 12.0], "low": [8.0, 11.0], "close": [9.0, 11.5],
    })
    # Second bar: high-low = 1, |high-prev_close| = 3, |low-prev_close| = 2 -> 3
    assert indicators.true_range(frame).iloc[-1] == pytest.approx(3.0)


def test_bollinger_bands_straddle_the_mean() -> None:
    series = pd.Series(np.random.default_rng(0).normal(100, 2, 100))
    bands = indicators.bollinger(series)
    assert bands["upper"].iloc[-1] > bands["mid"].iloc[-1] > bands["lower"].iloc[-1]


def test_obv_accumulates_with_the_direction_of_close() -> None:
    frame = pd.DataFrame({
        "close": [10.0, 11.0, 10.5, 12.0], "volume": [100.0, 200.0, 150.0, 300.0],
    })
    # +200 on the up bar, -150 on the down bar, +300 on the final up bar.
    assert indicators.obv(frame).iloc[-1] == pytest.approx(350.0)


def test_adx_is_high_in_a_trend_and_low_in_chop(trending_up, ranging) -> None:
    assert indicators.adx(trending_up)["adx"].iloc[-1] > 25
    assert indicators.adx(ranging)["adx"].iloc[-1] < 20


def test_compute_requires_enough_history() -> None:
    frame = pd.DataFrame({
        "open": [1.0] * 10, "high": [1.0] * 10, "low": [1.0] * 10,
        "close": [1.0] * 10, "volume": [1.0] * 10,
    })
    with pytest.raises(ValueError, match="at least 60 candles"):
        indicators.compute(frame)


def test_every_score_is_within_bounds(trending_up, ranging, squeeze) -> None:
    for frame in (trending_up, ranging, squeeze):
        ind = indicators.compute(frame)
        for name, score in ind.scores.items():
            assert -1.0 <= score <= 1.0, f"{name} out of bounds: {score}"


def test_uptrend_scores_bullish_on_trend_indicators(trending_up) -> None:
    ind = indicators.compute(trending_up)
    assert ind.scores["ema"] > 0
    assert ind.scores["macd"] > 0
    assert ind.scores["aroon"] > 0
    assert ind.cloud_position == 1


def test_squeeze_is_detected_when_volatility_contracts(squeeze) -> None:
    assert indicators.compute(squeeze).squeeze_on is True


def test_composite_is_bounded_and_reports_contributors(trending_up) -> None:
    ind = indicators.compute(trending_up)
    weights = {k: 1.0 for k in ind.scores}
    composite, contributors = indicators.composite_score(ind, weights)
    assert -1.0 <= composite <= 1.0
    assert len(contributors) == 3
    # Contributors are ordered by absolute contribution.
    magnitudes = [abs(c) for _, c in contributors]
    assert magnitudes == sorted(magnitudes, reverse=True)


def test_sentiment_cannot_dominate_the_composite(trending_up) -> None:
    ind = indicators.compute(trending_up)
    weights = {k: 1.0 for k in ind.scores}
    neutral, _ = indicators.composite_score(ind, weights, sentiment=0.0, sentiment_weight=0.15)
    hostile, _ = indicators.composite_score(ind, weights, sentiment=-1.0, sentiment_weight=0.15)
    # A maximally bearish sentiment reading may nudge the composite but must
    # never flip a strong technical signal on its own.
    assert abs(neutral - hostile) < 0.2


def test_zero_weights_produce_a_neutral_composite(ranging) -> None:
    ind = indicators.compute(ranging)
    ind.obv_divergence = 0
    composite, _ = indicators.composite_score(ind, {})
    assert composite == pytest.approx(0.0)


def test_divergence_flag_is_one_of_three_values(trending_up) -> None:
    assert indicators.compute(trending_up).obv_divergence in (-1, 0, 1)
