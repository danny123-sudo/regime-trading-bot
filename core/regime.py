"""Regime classifier: TRENDING / RANGING / BREAKOUT_WATCH / AMBIGUOUS.

Re-evaluated on every 15-minute candle close. The regime selects the indicator
weight profile used for the composite, and gates which strategy may fire.
ADX is the master filter: no trend-following below 20, no mean reversion above 30.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.indicators import IndicatorSet


class Regime(str, Enum):
    TRENDING = "TRENDING"
    RANGING = "RANGING"
    BREAKOUT_WATCH = "BREAKOUT_WATCH"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass
class RegimeResult:
    """Classification outcome for one pair on one candle close."""

    regime: Regime
    confidence: float          # 0..1, how cleanly the conditions are met
    direction: int             # +1 bullish, -1 bearish, 0 neutral
    reason: str

    @property
    def tradeable(self) -> bool:
        return self.regime is not Regime.AMBIGUOUS


def classify(ind: IndicatorSet, prev_adx: float | None = None) -> RegimeResult:
    """Classify the current regime from the indicator set.

    Args:
        ind: Indicators computed on the just-closed 15m candle.
        prev_adx: ADX from the previous candle, used to detect a rising ADX
            for BREAKOUT_WATCH. None on the first candle after startup.

    Returns:
        RegimeResult. AMBIGUOUS means hold — the bot never trades an unclear tape.
    """
    adx = ind.adx
    direction = 1 if ind.plus_di > ind.minus_di else -1
    adx_rising = prev_adx is not None and adx > prev_adx

    # BREAKOUT_WATCH takes precedence: a squeeze with ADX waking up is the
    # highest-conviction setup in the system and must not be misread as RANGING.
    if ind.squeeze_on and adx < 25 and (adx_rising or adx >= 20):
        confidence = 0.6 + (0.2 if adx_rising else 0.0) + (0.2 if ind.volume_ratio > 1.3 else 0.0)
        return RegimeResult(
            Regime.BREAKOUT_WATCH, min(confidence, 1.0), direction,
            f"BB inside Keltner, ADX {adx:.1f}{' rising' if adx_rising else ''}",
        )

    # TRENDING: strong directional move, price extended outside the bands and
    # confirmed by the cloud on the same side as DI dominance.
    if adx > 25:
        outside_bands = ind.bb_position > 0.9 or ind.bb_position < 0.1
        cloud_agrees = ind.cloud_position == direction
        if outside_bands or cloud_agrees:
            confidence = min(0.5 + (adx - 25) / 50.0 + (0.2 if cloud_agrees else 0.0), 1.0)
            return RegimeResult(
                Regime.TRENDING, confidence, direction,
                f"ADX {adx:.1f} > 25, cloud {'agrees' if cloud_agrees else 'neutral'}",
            )

    # RANGING: no trend, price oscillating inside the bands.
    if adx < 20 and 0.05 < ind.bb_position < 0.95:
        confidence = min(0.5 + (20 - adx) / 40.0, 1.0)
        return RegimeResult(
            Regime.RANGING, confidence, 0,
            f"ADX {adx:.1f} < 20, price inside Bollinger",
        )

    return RegimeResult(
        Regime.AMBIGUOUS, 0.0, 0,
        f"ADX {adx:.1f} in no-man's land or conditions unclear",
    )


def strategy_allows_entry(
    regime: RegimeResult,
    ind: IndicatorSet,
    side: str,
    volume_filter_mult: float,
) -> tuple[bool, str]:
    """Regime-specific entry preconditions, applied before the Grok call.

    These encode the quant rules that no LLM judgement may bypass:
    volume confirmation for mean reversion, volume agreement for breakouts,
    and the ADX bounds for each strategy family.

    Returns:
        (allowed, reason_if_not)
    """
    bullish = side == "long"

    if regime.regime is Regime.AMBIGUOUS:
        return False, "regime ambiguous"

    if regime.regime is Regime.TRENDING:
        if ind.adx < 20:
            return False, "trend entry requires ADX >= 20"
        if (bullish and regime.direction != 1) or (not bullish and regime.direction != -1):
            return False, "entry against trend direction"
        return True, ""

    if regime.regime is Regime.RANGING:
        if ind.adx > 30:
            return False, "mean reversion requires ADX <= 30"
        if ind.volume_ratio < volume_filter_mult:
            return False, (
                f"mean reversion needs volume >= {volume_filter_mult:.2f}x average, "
                f"got {ind.volume_ratio:.2f}x"
            )
        if bullish and ind.bb_position > 0.5:
            return False, "long mean reversion requires price in lower half of band"
        if not bullish and ind.bb_position < 0.5:
            return False, "short mean reversion requires price in upper half of band"
        return True, ""

    # BREAKOUT_WATCH: volume must confirm the direction of the release.
    volume_confirms = (
        ind.scores["obv"] > 0 and ind.scores["ad"] > 0 if bullish
        else ind.scores["obv"] < 0 and ind.scores["ad"] < 0
    )
    if not volume_confirms:
        return False, "breakout not confirmed by OBV/AD volume bias"
    if ind.volume_ratio < volume_filter_mult:
        return False, f"breakout needs volume expansion, got {ind.volume_ratio:.2f}x"
    return True, ""


def uses_momentum_orientation(regime: RegimeResult) -> bool:
    """Whether oscillators should be read as continuation rather than reversal.

    Trends and breakout releases ride momentum; ranges fade it.
    """
    return regime.regime in (Regime.TRENDING, Regime.BREAKOUT_WATCH)


def size_multiplier_for(regime: RegimeResult) -> float:
    """BREAKOUT_WATCH is the highest-conviction setup and sizes up to 1.5x."""
    return 1.5 if regime.regime is Regime.BREAKOUT_WATCH else 1.0
