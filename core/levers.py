"""The lever constitution: what the strategy council may tune, and its bounds.

Bounds are code; values are data. The Grok Bot council writes values into
``user_data/strategy_config.json``; it can never widen a bound, because doing
so would require changing this file, which is a human-plus-git operation.

The asymmetry is deliberate: every lever can move toward less risk freely and
toward more risk only up to a ceiling the council cannot lift.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

ROOT: Final[Path] = Path(__file__).resolve().parent.parent
USER_DATA: Final[Path] = ROOT / "user_data"
CONTEXT_DIR: Final[Path] = ROOT / "context"
STRATEGY_CONFIG_PATH: Final[Path] = USER_DATA / "strategy_config.json"
STRATEGY_CONFIG_PREV_PATH: Final[Path] = USER_DATA / "strategy_config.prev.json"
PROPOSAL_PATH: Final[Path] = CONTEXT_DIR / "strategy_proposal.json"
DAILY_BRIEF_PATH: Final[Path] = CONTEXT_DIR / "daily_brief.json"
ADAPTIVE_STATE_PATH: Final[Path] = USER_DATA / "adaptive_state.json"

# --- constants the council can never touch --------------------------------
MIN_CONFIDENCE_FLOOR: Final[float] = 0.70
MAX_POSITION_PCT: Final[float] = 0.08
KELLY_FRACTION: Final[float] = 0.25

ADAPTIVE_CHANGE_COOLDOWN_SEC: Final[int] = 72 * 3600
ADAPTIVE_MAX_LEVERS_PER_CHANGE: Final[int] = 3
ADAPTIVE_REVERT_WINDOW_SEC: Final[int] = 72 * 3600
ADAPTIVE_REVERT_WINDOW_TRADES: Final[int] = 20
ADAPTIVE_FREEZE_AFTER_REVERTS: Final[int] = 2
ADAPTIVE_BACKTEST_DAYS: Final[int] = 30
DAILY_BRIEF_MAX_AGE_SEC: Final[int] = 36 * 3600
SENTIMENT_MAX_AGE_SEC: Final[int] = 90 * 60

GROK_TIMEOUT_SEC: Final[int] = 20
GROK_FAILURE_ALERT_THRESHOLD: Final[int] = 3

# --- the lever board -------------------------------------------------------
DEFAULT_LEVERS: Final[dict] = {
    "composite_threshold": 0.65,
    "sentiment_weight": 0.10,
    "atr_stop_mult": 1.5,
    "trail_after_r": 1.0,
    "volume_filter_mult": 1.3,
    "min_confidence": 0.70,
    "daily_risk_budget": 0.02,
    "size_multiplier": 1.0,
    "regime_enabled": {"TRENDING": True, "RANGING": True, "BREAKOUT_WATCH": True},
    "regime_weights": {
        "TRENDING": {
            "ema": 1.0, "macd": 1.0, "aroon": 0.8, "psar": 0.7, "ichimoku": 1.0,
            "rsi": 0.3, "stoch": 0.3, "williams": 0.2, "adx": 0.6,
            "obv": 0.6, "ad": 0.4, "vwap": 0.3, "bb": 0.3, "squeeze": 0.2,
        },
        "RANGING": {
            "ema": 0.2, "macd": 0.2, "aroon": 0.3, "psar": 0.0, "ichimoku": 0.5,
            "rsi": 1.0, "stoch": 1.0, "williams": 0.8, "adx": 0.2,
            "obv": 0.4, "ad": 0.4, "vwap": 0.9, "bb": 1.0, "squeeze": 0.2,
        },
        "BREAKOUT_WATCH": {
            "ema": 0.4, "macd": 0.4, "aroon": 0.5, "psar": 0.2, "ichimoku": 0.6,
            "rsi": 0.3, "stoch": 0.3, "williams": 0.2, "adx": 0.7,
            "obv": 1.0, "ad": 1.0, "vwap": 0.4, "bb": 0.6, "squeeze": 1.0,
        },
    },
}

LEVER_BOUNDS: Final[dict[str, tuple[float, float]]] = {
    "composite_threshold": (0.30, 0.80),
    "sentiment_weight": (0.0, 0.15),
    "atr_stop_mult": (1.0, 2.5),
    "trail_after_r": (0.0, 3.0),
    "volume_filter_mult": (1.0, 2.0),
    "min_confidence": (0.70, 0.90),
    # Budget in risk, not trade count: five trades with 3% stops is triple the
    # exposure of five with 1% stops, so a tally is the wrong unit. A wide stop
    # consumes more budget and permits fewer trades, automatically.
    "daily_risk_budget": (0.0, 0.06),
    "size_multiplier": (0.25, 1.0),
}
INDICATOR_WEIGHT_BOUNDS: Final[tuple[float, float]] = (0.0, 1.0)

# Presence of any of these anywhere in a proposal rejects the whole thing.
FORBIDDEN_LEVER_KEYS: Final[frozenset[str]] = frozenset({
    "MAX_POSITION_PCT", "DAILY_LOSS_HALT_PCT", "TOTAL_DRAWDOWN_KILL_PCT",
    "MAX_LEVERAGE", "LIQ_BUFFER_ATR_MULT", "MIN_LIQUIDITY_USDT",
    "MIN_TRADE_INTERVAL_SEC", "MAX_DAILY_TRADES", "MODE", "mode",
    "PAPER_MODE", "paper_mode", "KELLY_FRACTION", "leverage", "margin_mode",
    "stoploss", "max_open_trades", "stake_amount", "dry_run",
    "api_key", "secret", "passphrase",
})


def ensure_dirs() -> None:
    """Create the directories the council and adaptive layer write into."""
    CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    USER_DATA.mkdir(parents=True, exist_ok=True)
