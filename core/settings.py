"""Environment settings and KuCoin symbol assertions.

Freqtrade owns exchange credentials, pairs, and stake configuration via
``user_data/config.json``. This module covers what freqtrade does not: the xAI
and Telegram-adjacent keys our own layers need, and the venue symbol
assertions that catch KuCoin's BTC/XBT trap.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Final

from dotenv import load_dotenv

from core.levers import ROOT

load_dotenv(ROOT / ".env")


class Mode(str, Enum):
    """Risk mode. CONSERVATIVE is spot long-only; AGGRESSIVE adds perp shorts."""

    CONSERVATIVE = "conservative"
    AGGRESSIVE = "aggressive"


# KuCoin spot uses BTC; KuCoin futures uses XBT. Wrong form = silent failure.
FUTURES_SYMBOL_OVERRIDES: Final[dict[str, str]] = {"BTC": "XBT"}


class SymbolError(ValueError):
    """Raised when a symbol is in the wrong form for its venue."""


def assert_spot_symbol(symbol: str) -> str:
    """Validate a KuCoin *spot* symbol (e.g. ``BTC-USDT``)."""
    if "XBT" in symbol.upper():
        raise SymbolError(f"Spot symbol must use BTC, not XBT: {symbol!r}")
    if "-" not in symbol:
        raise SymbolError(f"Spot symbol must be dash-separated, e.g. BTC-USDT: {symbol!r}")
    return symbol


def assert_futures_symbol(symbol: str) -> str:
    """Validate a KuCoin *futures* symbol (e.g. ``XBTUSDTM``)."""
    upper = symbol.upper()
    if "BTC" in upper:
        raise SymbolError(f"Futures symbol must use XBT, not BTC: {symbol!r}")
    if "-" in symbol or "/" in symbol:
        raise SymbolError(f"Futures symbol must have no separator: {symbol!r}")
    if not upper.endswith("M"):
        raise SymbolError(f"KuCoin USDT-margined futures symbols end in M: {symbol!r}")
    return symbol


def native_symbol(pair: str, mode: Mode = Mode.CONSERVATIVE) -> str:
    """Convert a ccxt pair (``BTC/USDT``) to the venue-native form, asserted.

    Freqtrade speaks ccxt symbols throughout, so this exists for the places we
    talk to KuCoin or its docs directly, and as a guard in tests.
    """
    base, _, quote = pair.partition("/")
    quote = quote.split(":")[0]
    if not base or not quote:
        raise SymbolError(f"Unrecognised pair {pair!r}")
    if mode is Mode.CONSERVATIVE:
        return assert_spot_symbol(f"{base}-{quote}")
    base = FUTURES_SYMBOL_OVERRIDES.get(base, base)
    return assert_futures_symbol(f"{base}{quote}M")


@dataclass(frozen=True)
class Settings:
    """Settings for the layers freqtrade does not own."""

    xai_api_key: str
    xai_model: str
    telegram_bot_token: str
    telegram_chat_id: str
    council_secret: str
    mode: Mode


class ConfigError(RuntimeError):
    """Raised when the environment is incomplete or inconsistent."""


def load_settings(require_xai: bool = True) -> Settings:
    """Load settings from the environment, failing loudly and by name."""
    raw_mode = os.getenv("MODE", "conservative").strip().lower()
    try:
        mode = Mode(raw_mode)
    except ValueError as exc:
        raise ConfigError(f"MODE must be 'conservative' or 'aggressive', got {raw_mode!r}") from exc

    xai_key = os.getenv("XAI_API_KEY", "").strip()
    if require_xai and not xai_key:
        raise ConfigError("Missing required environment variable: XAI_API_KEY")

    return Settings(
        xai_api_key=xai_key,
        xai_model=os.getenv("XAI_MODEL", "grok-4.6").strip(),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        council_secret=os.getenv("COUNCIL_SECRET", "").strip(),
        mode=mode,
    )
