"""Regime classification and the entry preconditions each regime enforces."""

from __future__ import annotations

from core import indicators
from core.regime import Regime, classify, size_multiplier_for, strategy_allows_entry


def _ind(**overrides):
    """Build an IndicatorSet with sane defaults, overriding what a test needs."""
    defaults = dict(
        close=100.0, atr=1.0, adx=15.0, plus_di=20.0, minus_di=20.0, rsi=50.0,
        bb_position=0.5, bb_upper=105.0, bb_lower=95.0, cloud_position=0,
        squeeze_on=False, volume_ratio=1.0, obv_divergence=0,
        scores={"obv": 0.0, "ad": 0.0}, raw={},
    )
    defaults.update(overrides)
    return indicators.IndicatorSet(**defaults)


def test_trending_requires_adx_above_25() -> None:
    result = classify(_ind(adx=32.0, plus_di=30.0, minus_di=10.0, cloud_position=1))
    assert result.regime is Regime.TRENDING
    assert result.direction == 1


def test_trending_detects_a_downtrend() -> None:
    result = classify(_ind(adx=32.0, plus_di=10.0, minus_di=30.0, cloud_position=-1))
    assert result.regime is Regime.TRENDING
    assert result.direction == -1


def test_ranging_requires_low_adx_and_price_inside_the_bands() -> None:
    result = classify(_ind(adx=14.0, bb_position=0.5))
    assert result.regime is Regime.RANGING
    assert result.direction == 0


def test_breakout_watch_on_a_squeeze_with_rising_adx() -> None:
    result = classify(_ind(adx=19.0, squeeze_on=True), prev_adx=15.0)
    assert result.regime is Regime.BREAKOUT_WATCH


def test_squeeze_takes_precedence_over_ranging() -> None:
    # Both conditions look true; the higher-conviction setup must win.
    result = classify(_ind(adx=18.0, squeeze_on=True, bb_position=0.5), prev_adx=12.0)
    assert result.regime is Regime.BREAKOUT_WATCH


def test_adx_between_20_and_25_is_ambiguous() -> None:
    result = classify(_ind(adx=22.0, bb_position=0.5))
    assert result.regime is Regime.AMBIGUOUS
    assert result.tradeable is False


def test_ambiguous_regime_blocks_every_entry() -> None:
    reg = classify(_ind(adx=22.0))
    allowed, reason = strategy_allows_entry(reg, _ind(adx=22.0), "long", 1.3)
    assert not allowed and "ambiguous" in reason


def test_mean_reversion_requires_the_volume_filter() -> None:
    ind = _ind(adx=14.0, bb_position=0.2, volume_ratio=1.05)
    reg = classify(ind)
    allowed, reason = strategy_allows_entry(reg, ind, "long", 1.3)
    assert not allowed and "volume" in reason


def test_mean_reversion_passes_with_volume_and_low_band_position() -> None:
    ind = _ind(adx=14.0, bb_position=0.2, volume_ratio=1.5)
    reg = classify(ind)
    allowed, _ = strategy_allows_entry(reg, ind, "long", 1.3)
    assert allowed


def test_long_mean_reversion_rejected_near_the_upper_band() -> None:
    ind = _ind(adx=14.0, bb_position=0.8, volume_ratio=1.5)
    reg = classify(ind)
    allowed, reason = strategy_allows_entry(reg, ind, "long", 1.3)
    assert not allowed and "lower half" in reason


def test_trend_entry_against_the_trend_is_rejected() -> None:
    ind = _ind(adx=32.0, plus_di=30.0, minus_di=10.0, cloud_position=1)
    reg = classify(ind)
    allowed, reason = strategy_allows_entry(reg, ind, "short", 1.3)
    assert not allowed and "against trend" in reason


def test_breakout_requires_volume_confirmation_in_the_trade_direction() -> None:
    ind = _ind(adx=19.0, squeeze_on=True, volume_ratio=2.0,
               scores={"obv": -0.5, "ad": -0.4})
    reg = classify(ind, prev_adx=15.0)
    allowed, reason = strategy_allows_entry(reg, ind, "long", 1.3)
    assert not allowed and "volume bias" in reason


def test_breakout_allowed_when_volume_agrees() -> None:
    ind = _ind(adx=19.0, squeeze_on=True, volume_ratio=2.0,
               scores={"obv": 0.6, "ad": 0.5})
    reg = classify(ind, prev_adx=15.0)
    allowed, _ = strategy_allows_entry(reg, ind, "long", 1.3)
    assert allowed


def test_breakout_watch_sizes_up_and_others_do_not() -> None:
    breakout = classify(_ind(adx=19.0, squeeze_on=True), prev_adx=15.0)
    trending = classify(_ind(adx=32.0, plus_di=30.0, minus_di=10.0, cloud_position=1))
    assert size_multiplier_for(breakout) == 1.5
    assert size_multiplier_for(trending) == 1.0


def test_classifier_runs_on_real_fixtures(trending_up, ranging) -> None:
    up = classify(indicators.compute(trending_up))
    flat = classify(indicators.compute(ranging))
    assert up.regime is Regime.TRENDING and up.direction == 1
    assert flat.regime in (Regime.RANGING, Regime.BREAKOUT_WATCH)
