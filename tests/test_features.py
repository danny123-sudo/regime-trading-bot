"""Parity between the vectorised feature path and the scalar reference.

``core/features.py`` exists so freqtrade can compute a whole column at once
instead of per candle. That speed is only worth having if it computes the same
thing, so these tests pin the two implementations against each other.
"""

from __future__ import annotations

import pytest

from core import features, indicators
from core.regime import Regime, classify


def _last(df, column):
    return float(df[column].iloc[-1])


def test_regime_codes_round_trip() -> None:
    for code, name in features.REGIME_NAMES.items():
        assert features.NAME_TO_CODE[name] == code


def test_populate_adds_every_expected_column(trending_up) -> None:
    out = features.populate(trending_up)
    for indicator in features.SCORE_COLUMNS:
        assert f"s_{indicator}" in out, f"missing s_{indicator}"
    for column in ("regime", "composite", "atr", "adx", "bb_position",
                   "cloud_position", "volume_ratio", "obv_divergence"):
        assert column in out, f"missing {column}"


def test_scores_stay_within_bounds(trending_up, ranging, squeeze) -> None:
    for frame in (trending_up, ranging, squeeze):
        out = features.populate(frame)
        for indicator in features.SCORE_COLUMNS:
            column = out[f"s_{indicator}"]
            assert column.min() >= -1.0 and column.max() <= 1.0, indicator
        assert out["composite"].min() >= -1.0
        assert out["composite"].max() <= 1.0


@pytest.mark.parametrize("fixture_name", ["trending_up", "ranging", "squeeze"])
def test_vectorised_matches_scalar_indicators(request, fixture_name) -> None:
    """Core indicator values must agree with the per-candle implementation."""
    frame = request.getfixturevalue(fixture_name)
    vector = features.populate(frame)
    scalar = indicators.compute(frame)

    assert _last(vector, "atr") == pytest.approx(scalar.atr, rel=1e-9)
    assert _last(vector, "adx") == pytest.approx(scalar.adx, rel=1e-9)
    assert _last(vector, "rsi") == pytest.approx(scalar.rsi, rel=1e-9)
    assert _last(vector, "bb_position") == pytest.approx(scalar.bb_position, rel=1e-9)
    assert int(vector["cloud_position"].iloc[-1]) == scalar.cloud_position
    assert bool(vector["squeeze_on"].iloc[-1]) == scalar.squeeze_on
    assert _last(vector, "volume_ratio") == pytest.approx(scalar.volume_ratio, rel=1e-9)


@pytest.mark.parametrize("fixture_name", ["trending_up", "ranging", "squeeze"])
def test_vectorised_regime_matches_scalar(request, fixture_name) -> None:
    """The vectorised classifier must reach the same verdict as the scalar one."""
    frame = request.getfixturevalue(fixture_name)
    vector = features.populate(frame)
    scalar = indicators.compute(frame)
    # The scalar classifier needs the previous candle's ADX for a rising check.
    prev_adx = float(vector["adx"].iloc[-2])
    expected: Regime = classify(scalar, prev_adx).regime

    assert features.REGIME_NAMES[int(vector["regime"].iloc[-1])] == expected.value


def test_reversion_and_momentum_orientations_disagree_in_a_trend(trending_up) -> None:
    """The bug that made the composite unreachable: both readings must exist.

    In an uptrend, RSI read as reversion is bearish and read as continuation is
    bullish. Blending them in one composite made trend and momentum cancel.
    """
    out = features.populate(trending_up)
    assert _last(out, "s_rsi") < 0 < _last(out, "m_rsi")


def test_recompute_composite_responds_to_weights(trending_up) -> None:
    out = features.populate(trending_up)
    trend_only = {"TRENDING": {"ema": 1.0}, "RANGING": {"ema": 1.0},
                  "BREAKOUT_WATCH": {"ema": 1.0}}
    reweighted = features.recompute_composite(out, trend_only)
    assert not reweighted.equals(out["composite"])


def test_divergence_flag_is_ternary(trending_up) -> None:
    values = set(features.populate(trending_up)["obv_divergence"].unique())
    assert values <= {-1, 0, 1}
