"""Shared fixtures: synthetic candle series with known regime characteristics."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Tests must never depend on a real .env.
os.environ.setdefault("XAI_API_KEY", "test-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "12345")
os.environ.setdefault("PAPER_MODE", "true")
os.environ.setdefault("MODE", "conservative")


def _frame(closes: np.ndarray, volume: np.ndarray | None = None) -> pd.DataFrame:
    """Build an OHLCV frame from a close series with plausible highs and lows."""
    n = len(closes)
    noise = np.abs(np.sin(np.arange(n) * 0.7)) * closes * 0.002 + closes * 0.001
    volume = volume if volume is not None else np.full(n, 1000.0)
    return pd.DataFrame({
        "ts": np.arange(n) * 900_000,
        "open": np.concatenate([[closes[0]], closes[:-1]]),
        "high": closes + noise,
        "low": closes - noise,
        "close": closes,
        "volume": volume,
    })


@pytest.fixture
def trending_up() -> pd.DataFrame:
    """A clean uptrend: high ADX, price above the cloud."""
    n = 400
    base = np.linspace(100.0, 200.0, n)
    wobble = np.sin(np.arange(n) * 0.3) * 0.4
    return _frame(base + wobble)


@pytest.fixture
def ranging() -> pd.DataFrame:
    """A choppy, directionless market: low ADX, price inside the bands.

    Deliberately noise rather than a clean sine wave — a smooth oscillation
    contains multi-candle directional runs that lift ADX above 20, which is a
    trend by the classifier's definition and not what this fixture means.
    """
    rng = np.random.default_rng(7)
    closes = 100.0 + np.cumsum(rng.normal(0.0, 0.15, 400))
    closes -= np.linspace(0.0, closes[-1] - closes[0], 400)  # remove residual drift
    return _frame(closes)


@pytest.fixture
def squeeze() -> pd.DataFrame:
    """Volatility contraction: wide early, very tight late (BB inside Keltner)."""
    n = 400
    wide = 100.0 + np.sin(np.arange(300) * 0.4) * 6.0
    tight = wide[-1] + np.sin(np.arange(100) * 0.4) * 0.05
    return _frame(np.concatenate([wide, tight]))


@pytest.fixture
def settings():
    from core import settings as config
    return config.load_settings()
