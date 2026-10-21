"""Symbol assertion tests.

KuCoin spot uses BTC; KuCoin futures uses XBT. Getting this backwards causes
silent order failures, so the assertions are tested in both directions.
"""

from __future__ import annotations

import pytest

from core import settings as config


def test_spot_rejects_xbt() -> None:
    with pytest.raises(config.SymbolError):
        config.assert_spot_symbol("XBT-USDT")


def test_spot_requires_dash() -> None:
    with pytest.raises(config.SymbolError):
        config.assert_spot_symbol("BTCUSDT")


def test_spot_accepts_correct_form() -> None:
    assert config.assert_spot_symbol("BTC-USDT") == "BTC-USDT"
    assert config.assert_spot_symbol("ETH-USDT") == "ETH-USDT"


def test_futures_rejects_btc() -> None:
    with pytest.raises(config.SymbolError):
        config.assert_futures_symbol("BTCUSDTM")


def test_futures_rejects_separators() -> None:
    with pytest.raises(config.SymbolError):
        config.assert_futures_symbol("XBT-USDTM")


def test_futures_requires_m_suffix() -> None:
    with pytest.raises(config.SymbolError):
        config.assert_futures_symbol("XBTUSDT")


def test_futures_accepts_correct_form() -> None:
    assert config.assert_futures_symbol("XBTUSDTM") == "XBTUSDTM"
    assert config.assert_futures_symbol("ETHUSDTM") == "ETHUSDTM"


def test_native_symbol_picks_the_right_venue() -> None:
    assert config.native_symbol("BTC/USDT", config.Mode.CONSERVATIVE) == "BTC-USDT"
    assert config.native_symbol("BTC/USDT", config.Mode.AGGRESSIVE) == "XBTUSDTM"


def test_native_symbol_rejects_a_malformed_pair() -> None:
    with pytest.raises(config.SymbolError):
        config.native_symbol("NOTAPAIR")


def test_whitelisted_pairs_convert_to_both_venues() -> None:
    """Every pair we trade must survive conversion to both venue forms."""
    import json
    from pathlib import Path

    cfg = json.loads((Path(__file__).parent.parent / "user_data" / "config.json").read_text())
    pairs = cfg["exchange"]["pair_whitelist"]
    assert pairs, "config.json has an empty pair_whitelist"
    for pair in pairs:
        config.native_symbol(pair, config.Mode.CONSERVATIVE)
        config.native_symbol(pair, config.Mode.AGGRESSIVE)


def test_btc_maps_to_xbt_on_futures_only() -> None:
    """The whole point of the assertions: BTC on spot, XBT on futures."""
    assert config.native_symbol("BTC/USDT", config.Mode.CONSERVATIVE) == "BTC-USDT"
    assert config.native_symbol("BTC/USDT", config.Mode.AGGRESSIVE) == "XBTUSDTM"
    assert config.native_symbol("HYPE/USDT", config.Mode.CONSERVATIVE) == "HYPE-USDT"

