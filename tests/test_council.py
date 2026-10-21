"""Council airlock tests.

The council is the one component that can change live trading behaviour
without a human. These pin the boundaries: what it may move, how far, and what
it may never touch.
"""

from __future__ import annotations

import json

import pytest

from core import council


def test_defaults_are_the_validated_configs() -> None:
    assert council.DEFAULTS["CoreTrend"]["st_mult"] == 4.0
    assert council.DEFAULTS["CoreTrend"]["st_period"] == 14
    assert council.DEFAULTS["_global"]["size_multiplier"] == 1.0


def test_size_multiplier_can_only_reduce_risk() -> None:
    lo, hi = council.BOUNDS["_global"]["size_multiplier"]
    assert hi == 1.0, "1.0 must be the ceiling — the council cannot add leverage"
    assert lo > 0


def test_supertrend_bounds_are_tight() -> None:
    """(14, 4.0) returned +74% and (10, 3.0) returned -88% on identical data.

    The council gets a narrow band around the validated value, not free rein.
    """
    lo, hi = council.BOUNDS["CoreTrend"]["st_mult"]
    assert lo > 3.0 and hi < 5.0
    p_lo, p_hi = council.BOUNDS["CoreTrend"]["st_period"]
    assert p_lo > 10 and p_hi < 20


@pytest.mark.parametrize("key", [
    "dry_run", "stake_amount", "max_open_trades", "available_capital",
    "leverage", "api_key", "secret", "timeframe", "pair_whitelist",
])
def test_forbidden_keys_reject_the_whole_proposal(key) -> None:
    clean, msgs = council.validate_proposal({"CoreTrend": {"st_mult": 4.1, key: 99}})
    assert clean == {}, "a proposal touching protected settings must be rejected entirely"
    assert any("REJECTED" in m for m in msgs)


def test_nested_forbidden_key_is_caught() -> None:
    clean, msgs = council.validate_proposal(
        {"CoreTrend": {"st_mult": 4.1}, "nested": {"deeper": {"leverage": 10}}})
    assert clean == {}


def test_out_of_bounds_values_are_clamped_not_rejected() -> None:
    clean, msgs = council.validate_proposal({"CoreTrend": {"st_mult": 99.0}})
    assert clean["CoreTrend"]["st_mult"] == council.BOUNDS["CoreTrend"]["st_mult"][1]
    assert any("clamped" in m for m in msgs)


def test_unknown_lever_is_ignored_not_applied() -> None:
    clean, msgs = council.validate_proposal({"CoreTrend": {"made_up_lever": 1.0}})
    assert "made_up_lever" not in clean.get("CoreTrend", {})


def test_unknown_strategy_is_ignored() -> None:
    clean, msgs = council.validate_proposal({"NotAStrategy": {"st_mult": 4.0}})
    assert clean == {}


def test_enabled_flag_survives_validation() -> None:
    clean, _ = council.validate_proposal({"MomentumCore": {"enabled": False}})
    assert clean["MomentumCore"]["enabled"] is False


def test_missing_config_falls_back_to_defaults(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(council, "CONFIG_PATH", tmp_path / "absent.json")
    assert council.lever("CoreTrend", "st_mult") == 4.0
    assert council.size_multiplier() == 1.0
    assert council.is_enabled("CoreTrend") is True


def test_corrupt_config_falls_back_to_defaults(monkeypatch, tmp_path) -> None:
    bad = tmp_path / "strategy_config.json"
    bad.write_text("{not json")
    monkeypatch.setattr(council, "CONFIG_PATH", bad)
    assert council.lever("CoreTrend", "st_mult") == 4.0


def test_lever_clamps_a_hostile_config(monkeypatch, tmp_path) -> None:
    cfg = tmp_path / "strategy_config.json"
    cfg.write_text(json.dumps({"CoreTrend": {"st_mult": 999}}))
    monkeypatch.setattr(council, "CONFIG_PATH", cfg)
    assert council.lever("CoreTrend", "st_mult") == council.BOUNDS["CoreTrend"]["st_mult"][1]


def test_stand_down_is_readable(monkeypatch, tmp_path) -> None:
    cfg = tmp_path / "strategy_config.json"
    cfg.write_text(json.dumps({"MomentumCore": {"enabled": False}}))
    monkeypatch.setattr(council, "CONFIG_PATH", cfg)
    assert council.is_enabled("MomentumCore") is False
    assert council.is_enabled("CoreTrend") is True   # unaffected
